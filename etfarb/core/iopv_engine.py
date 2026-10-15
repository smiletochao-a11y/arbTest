# -*- coding: utf-8 -*-
"""
ETF 申赎套利核心计算引擎（纯标准库，零第三方依赖）。

设计原则
========
1. 不依赖任何行情源 / 终端库，可以在本地 Python 直接 import 和测试，
   也可以在大QMT 策略编辑器里 import（只要把 etf_arb 目录放在策略同目录）。
2. 所有"可成交"估值都用盘口价而不是最新成交价：
   - 申购方向（凑篮子成本）：成分股用【卖一价】，因为你要买进；
   - 赎回方向（篮子变现）：成分股用【买一价】，因为你要卖出。
   这是帖子"官方 IOPV 不可靠"教训的解法：官方 IOPV 用最新成交价 + 忽略现金替代。
3. 现金替代显式处理，不允许含糊：
   - 禁止现金替代(FORBIDDEN)：必须实物，直接用盘口价；
   - 允许现金替代(ALLOWED)：申购加替代溢价、赎回减替代溢价（T+2 退补，故有不确定性）；
   - 必须现金替代(MUST / MUST_REFUND)：停牌/跨市场成分，用固定替代金额或结算价估算，
     并把"金额不确定"作为风险点上报——这是套利亏损的主要来源。
4. 套利评估必须扣成本：佣金、印花税、滑点、最低佣金、现金差额风险预留。

典型用法
========
    from etf_arb.iopv_engine import evaluate, ETFInfo, CostModel, ...
    info  = load_pcf_from_qmt(...)      # 或直接用 dataclass 构造
    quotes = load_quotes(...)           # {code: Quote}
    signal = evaluate(info, quotes, etf_price, cost)
"""

from enum import IntEnum
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 申赎清单（PCF）与行情数据结构
# 注意：本模块必须兼容大QMT 内置的旧版 Python（约 3.6，不支持 dataclass /
#       from __future__ import annotations），故以下类全部手写 __init__。
# ---------------------------------------------------------------------------

class ReplaceFlag(IntEnum):
    """现金替代标志（沪深交易所标准：0/1/2/3）。"""
    FORBIDDEN = 0      # 禁止现金替代：必须交实物股票
    ALLOWED = 1        # 允许现金替代：可用现金，按替代溢价比例，T+2 退补
    MUST = 2           # 必须现金替代：只能交现金（停牌股、跨市场成分），按固定金额/结算价退补
    MUST_REFUND = 3    # 必须且退补现金替代（深市）：必须现金，多退少补（按实际结算价）


class Component:
    """申赎清单中的一个成分股（对应一个最小申赎单位的数量）。"""

    def __init__(self, code, name, volume, replace_flag=ReplaceFlag.FORBIDDEN,
                 replace_premium=0.10, fixed_amount=0.0, fixed_amount_redemption=0.0,
                 is_cross_market=False):
        self.code = code                              # 成分股代码，如 '600585.SH'
        self.name = name                              # 名称（仅展示用）
        self.volume = volume                          # 一个最小申赎单位所需股数
        self.replace_flag = replace_flag
        self.replace_premium = replace_premium        # 允许现金替代溢价比例（深市常见10%），0.10=10%
        self.fixed_amount = fixed_amount              # 申购方向固定替代金额（QMT ReplaceBalance），0=未知
        self.fixed_amount_redemption = fixed_amount_redemption  # 赎回方向固定替代金额（QMT redemptReplaceBalance）
        self.is_cross_market = is_cross_market        # 是否跨市场成分

    def __repr__(self):
        return ("Component(%s,%s,vol=%s,flag=%s)" %
                (self.code, self.name, self.volume, int(self.replace_flag)))


class ETFInfo:
    """一只 ETF 的申赎信息（来自当日 PCF 清单）。"""

    def __init__(self, etf_code, etf_name, report_unit, cash_balance=0.0,
                 enable_creation=True, enable_redemption=True, components=None,
                 nav_per_cu=0.0):
        self.etf_code = etf_code
        self.etf_name = etf_name
        self.report_unit = report_unit                # 最小申赎单位（份），如 900000
        self.cash_balance = cash_balance              # 预估现金差额 ECC（元/每篮子，T-1 公布）
        self.enable_creation = enable_creation        # 是否允许申购（跨境 ETF 常盘中暂停）
        self.enable_redemption = enable_redemption    # 是否允许赎回
        self.components = components if components is not None else []
        self.nav_per_cu = nav_per_cu                  # 每篮子净值（QMT navPerCU，T-1 口径，用于现金差额偏差估算）

    def __repr__(self):
        return ("ETFInfo(%s,%s,unit=%s,comp=%d)" %
                (self.etf_code, self.etf_name, self.report_unit, len(self.components)))


class Quote:
    """单只股票的实时盘口（自算 IOPV 需要 bid1/ask1）。"""

    def __init__(self, last=0.0, bid1=0.0, ask1=0.0, prev_close=0.0,
                 halted=False, limit_up=False, limit_down=False):
        self.last = last              # 最新成交价
        self.bid1 = bid1              # 买一价
        self.ask1 = ask1              # 卖一价
        self.prev_close = prev_close  # 昨收（用于判断涨跌停）
        self.halted = halted          # 停牌
        self.limit_up = limit_up      # 涨停（买不到/赎回补券危险）
        self.limit_down = limit_down  # 跌停（卖不出/假折价来源）

    def is_valid(self):
        return self.bid1 > 0 and self.ask1 > 0

    def __repr__(self):
        return ("Quote(last=%s,bid1=%s,ask1=%s)" % (self.last, self.bid1, self.ask1))


class CostModel:
    """套利交易成本模型（单位：基点 bps，1bp = 0.01%）。"""

    def __init__(self, commission_bps=1.0, min_commission=5.0, stamp_tax_bps=5.0,
                 slippage_bps=10.0, cash_diff_risk_bps=20.0,
                 fund_hold_days=3, annual_interest=0.03):
        self.commission_bps = commission_bps      # 券商佣金：万1
        self.min_commission = min_commission      # 单笔最低佣金（元）
        self.stamp_tax_bps = stamp_tax_bps        # 印花税（仅卖出股票收：万5；ETF 买卖免）
        self.slippage_bps = slippage_bps          # 滑点/冲击成本：万10
        self.cash_diff_risk_bps = cash_diff_risk_bps  # 现金差额不确定度预留：万20（帖子教训：主要亏损源）
        self.fund_hold_days = fund_hold_days      # 资金占用天数（T+2/T+3 交收）
        self.annual_interest = annual_interest    # 年化资金成本（如 3%）

    def stock_buy_fee(self, notional):
        return max(notional * self.commission_bps / 10000.0, self.min_commission)

    def stock_sell_fee(self, notional):
        return (max(notional * self.commission_bps / 10000.0, self.min_commission)
                + notional * self.stamp_tax_bps / 10000.0)

    def etf_buy_fee(self, notional):
        return max(notional * self.commission_bps / 10000.0, self.min_commission)

    def etf_sell_fee(self, notional):
        return max(notional * self.commission_bps / 10000.0, self.min_commission)

    def fund_cost(self, notional):
        """资金占用成本（按 days 计）。"""
        return notional * self.annual_interest * self.fund_hold_days / 365.0

    def cash_diff_risk(self, notional):
        """现金差额风险预留（元的量级）。"""
        return notional * self.cash_diff_risk_bps / 10000.0


class ArbSignal:
    """一次评估的结果。direction: 'premium'(溢价→申购套利) / 'discount'(折价→赎回套利) / 'none'。"""

    def __init__(self, direction, etf_price, iopv_mid, iopv_creation, iopv_redemption,
                 premium, pnl_per_unit, pnl_bps, cash_replace_ratio, reasons=None,
                 cash_bias=0.0, cash_bias_bps=0.0,
                 official_iopv=0.0, iopv_err_bps=0.0, trustworthy=True,
                 fee_etf=0.0, fee_basket=0.0, fee_creation=0.0, fee_redemption=0.0):  # [AI-2026-08-29] 新增四类费用字段（供看板展示）
        self.direction = direction
        self.etf_price = etf_price
        self.iopv_mid = iopv_mid                  # 自算：中间口径 IOPV（真实值，不被闸门覆盖）
        self.iopv_creation = iopv_creation        # 自算：凑篮子成本对应净值
        self.iopv_redemption = iopv_redemption    # 自算：篮子变现对应净值
        self.premium = premium                    # 溢价率（基于 iopv_mid）
        self.pnl_per_unit = pnl_per_unit          # 每个最小申赎单位预期净盈亏（已扣成本，元）
        self.pnl_bps = pnl_bps                    # 预期净盈亏占篮子市值（bps）
        self.cash_replace_ratio = cash_replace_ratio  # 现金替代市值占比（IOPV 可信度指标）
        self.reasons = reasons if reasons is not None else []   # 风险提示/为什么不做
        self.cash_bias = cash_bias                # 动态现金差额偏差（元）：预估ECC - 按当前价反推的实际差额
        self.cash_bias_bps = cash_bias_bps        # 同上，占篮子市值 bps
        self.official_iopv = official_iopv        # 官方 IOPV（对比基准）
        self.iopv_err_bps = iopv_err_bps          # 自算中 vs 官方 误差（bps），核心观察指标
        self.trustworthy = trustworthy              # 自算 IOPV 是否可信（闸门只控信号，不改IOPV）
        self.fee_etf = fee_etf                      # [AI-2026-08-29] 现券费用（ETF 现货双边买卖费）
        self.fee_basket = fee_basket                # [AI-2026-08-29] IOPV费用（篮子成分股双边买卖费）
        self.fee_creation = fee_creation            # [AI-2026-08-29] 申购费用（资金占用成本，creation 侧）
        self.fee_redemption = fee_redemption        # [AI-2026-08-29] 赎回费用（资金占用成本，redemption 侧）

    def __repr__(self):
        return ("ArbSignal(%s,premium=%s,pnl=%s)" %
                (self.direction, self.premium, self.pnl_bps))


# ---------------------------------------------------------------------------
# 成分价值计算
# ---------------------------------------------------------------------------

def component_cost_creation(comp: Component, q: Quote) -> Tuple[float, str]:
    """申购方向：凑齐该成分股所需的现金（元）。返回 (金额, 取价方式说明)。"""
    if comp.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND):
        if comp.fixed_amount > 0:
            return comp.fixed_amount, "固定替代金额(申购,含溢价)"
        if q.halted or q.last <= 0:
            return 0.0, "必须现金替代但金额未知(停牌)，按0估——风险！"
        # 无固定金额时按最新价估算，结算按 T+2 实际价，故不确定
        return q.last * comp.volume, "必须现金替代(无固定额)，按最新价估，T+2结算有偏差"
    if q.halted:
        return 0.0, "停牌但非必须替代——数据异常，跳过"
    if q.limit_up and q.ask1 <= 0:
        return 0.0, "涨停买不到(卖一为空)，套利不可执行"
    price = q.ask1 if q.ask1 > 0 else q.last
    if comp.replace_flag == ReplaceFlag.ALLOWED:
        return price * comp.volume * (1.0 + comp.replace_premium), "卖一价+替代溢价"
    return price * comp.volume, "卖一价"


def component_value_redemption(comp: Component, q: Quote) -> Tuple[float, str]:
    """赎回方向：卖掉该成分股能拿回的现金（元）。返回 (金额, 取价方式说明)。"""
    if comp.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND):
        amt = comp.fixed_amount_redemption if comp.fixed_amount_redemption > 0 else comp.fixed_amount
        if amt > 0:
            return amt, "固定替代金额(赎回)"
        if q.halted or q.last <= 0:
            return 0.0, "必须现金替代但金额未知(停牌)，按0估——风险！"
        return q.last * comp.volume, "必须现金替代(无固定额)，按最新价估，T+2结算有偏差"
    if q.halted:
        return 0.0, "停牌但非必须替代——数据异常，跳过"
    if q.limit_down and q.bid1 <= 0:
        return 0.0, "跌停卖不出(买一为空)，赎回篮子无法变现——假折价来源！"
    price = q.bid1 if q.bid1 > 0 else q.last
    if comp.replace_flag == ReplaceFlag.ALLOWED:
        return price * comp.volume * (1.0 - comp.replace_premium), "买一价-替代折价"
    return price * comp.volume, "买一价"


# ---------------------------------------------------------------------------
# 篮子级计算
# ---------------------------------------------------------------------------

def _component_value_fallback(comp: Component, direction: str) -> Tuple[float, bool]:
    """成分股缺行情时的兜底估值：有固定替代金额就用它（元宝AI 建议，优于直接记0）。
    返回 (金额, 是否命中替代金额)。"""
    amt = comp.fixed_amount_redemption if direction == "redemption" and comp.fixed_amount_redemption > 0 \
        else comp.fixed_amount
    if comp.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND, ReplaceFlag.ALLOWED) \
            and amt > 0:
        return amt, True
    return 0.0, False


def basket_cost_creation(info: ETFInfo, quotes: Dict[str, Quote]) -> float:
    """申购方向：凑齐整个篮子的真实现金成本（元）。缺行情成分用替代金额兜底。"""
    total = 0.0
    for c in info.components:
        if c.code in quotes:
            total += component_cost_creation(c, quotes[c.code])[0]
        else:
            total += _component_value_fallback(c, "creation")[0]
    return total


def basket_value_redemption(info: ETFInfo, quotes: Dict[str, Quote]) -> float:
    """赎回方向：整个篮子的真实变现价值（元）。缺行情成分用替代金额兜底。"""
    total = 0.0
    for c in info.components:
        if c.code in quotes:
            total += component_value_redemption(c, quotes[c.code])[0]
        else:
            total += _component_value_fallback(c, "redemption")[0]
    return total


def iopv_creation(info: ETFInfo, quotes: Dict[str, Quote]) -> float:
    """自算 IOPV（申购视角，凑篮子成本/最小申赎单位）。"""
    total = basket_cost_creation(info, quotes) + info.cash_balance
    return total / info.report_unit if info.report_unit > 0 else 0.0


def iopv_redemption(info: ETFInfo, quotes: Dict[str, Quote]) -> float:
    """自算 IOPV（赎回视角，篮子变现/最小申赎单位）。"""
    total = basket_value_redemption(info, quotes) + info.cash_balance
    return total / info.report_unit if info.report_unit > 0 else 0.0


def premium_rate(etf_price: float, iopv: float) -> float:
    """溢价率（正=溢价，负=折价）。"""
    if iopv <= 0:
        return 0.0
    return (etf_price - iopv) / iopv


def cash_replace_ratio(info: ETFInfo, quotes: Dict[str, Quote]) -> float:
    """现金替代市值占比 = (必须+允许现金替代成分市值) / 篮子总市值。
    帖子教训：占比越高，IOPV 与实际净值偏差越大，信号越不可信。"""
    total = 0.0
    replaced = 0.0
    for c in info.components:
        q = quotes.get(c.code)
        if q is None or q.last <= 0:
            continue
        v = c.volume * q.last
        total += v
        if c.replace_flag in (ReplaceFlag.ALLOWED, ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND):
            replaced += v
    return replaced / total if total > 0 else 0.0


def cash_diff_bias(info: ETFInfo, quotes: Dict[str, Quote]) -> Tuple[float, float]:
    """动态现金差额偏差（借鉴元宝AI 的 cash_diff_diagnosis 思路）。

    原理：若按当前价收盘，实际现金差额 ≈ 每篮子净值(navPerCU) − 实时股票篮子市值。
    bias = 预估现金差额(ECC) − 反推的实际现金差额
    正 bias：按当前价收盘，实际差额会比预估少 → 折价赎回方会被多扣（帖子作者踩的坑）。
    大涨时实时篮子市值高 → 反推差额变小 → bias 变大；大跌相反。方向与帖子经验一致。

    返回 (bias元, bias占篮子bps)。
    """
    if info.nav_per_cu <= 0:
        return 0.0, 0.0
    stock_value = 0.0
    for c in info.components:
        q = quotes.get(c.code)
        if q is not None and q.last > 0:
            stock_value += c.volume * q.last
        else:
            stock_value += _component_value_fallback(c, "creation")[0]
    implied_actual_cash = info.nav_per_cu - stock_value
    bias = info.cash_balance - implied_actual_cash
    bps = bias / info.nav_per_cu * 10000.0 if info.nav_per_cu > 0 else 0.0
    return round(bias, 2), round(bps, 1)


# ---------------------------------------------------------------------------
# 套利评估（扣成本后的真实信号）
# ---------------------------------------------------------------------------

def evaluate(
    info: ETFInfo,
    quotes: Dict[str, Quote],
    etf_price: float,
    cost: Optional[CostModel] = None,
    official_iopv: Optional[float] = None,
) -> ArbSignal:
    """对一只 ETF 做完整套利评估。

    - 溢价套利：卖 ETF 得款 > 凑篮子成本 + 交易费用 + 现金差额风险 + 资金成本
    - 折价套利：篮子变现 > 买 ETF 花费 + 交易费用 + 现金差额风险 + 资金成本
    - 安全闸门：成分行情覆盖率 < 50%（如 QDII 海外成分无行情）→ 禁止出信号，
      降级用官方 IOPV 展示溢价率供择时。official_iopv 传入则用于降级展示。
    """
    cost = cost or CostModel()
    reasons: List[str] = []
    unit = info.report_unit

    # 注意：真实成本/变现都必须含预估现金差额 ECC（申购要交 ECC、赎回收回 ECC）
    basket_c = basket_cost_creation(info, quotes) + info.cash_balance
    basket_r = basket_value_redemption(info, quotes) + info.cash_balance
    iopv_c = basket_c / unit if unit else 0.0
    iopv_r = basket_r / unit if unit else 0.0
    iopv_mid = (iopv_c + iopv_r) / 2.0
    prem = premium_rate(etf_price, iopv_mid)
    etf_notional = etf_price * unit

    # ---- 先做硬性检查 ----
    if not info.enable_creation:
        reasons.append("申购暂停，溢价方向不可执行")
    if not info.enable_redemption:
        reasons.append("赎回暂停，折价方向不可执行")
    if unit <= 0 or etf_price <= 0:
        reasons.append("最小申赎单位或 ETF 价格无效")
    # 涨跌停重仓检查
    for c in info.components:
        q = quotes.get(c.code)
        if q is None:
            continue
        if q.limit_up and c.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND):
            reasons.append(f"成分 {c.name}({c.code}) 涨停且为必须现金替代——次日补券价不可控")
        if q.limit_down and c.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND):
            reasons.append(f"成分 {c.name}({c.code}) 跌停且为必须现金替代——按跌停价结算=假折价来源")
        if q.limit_down and c.replace_flag == ReplaceFlag.FORBIDDEN:
            reasons.append(f"成分 {c.name}({c.code}) 跌停(禁止替代)——赎回后卖不出")
        if (c.replace_flag in (ReplaceFlag.MUST, ReplaceFlag.MUST_REFUND)
                and q.halted and c.fixed_amount <= 0):
            reasons.append(f"成分 {c.name}({c.code}) 必须现金替代且停牌、无固定替代金额"
                           f"——篮子估值失真(按0估)，信号不可信！")

    # ---- 覆盖率 + 与官方 IOPV 对比误差（核心观察）+ 可信度判定（不拦截，只控信号）----
    total = len(info.components)
    valid = 0
    for c in info.components:
        q = quotes.get(c.code)
        if (q is not None and q.last > 0) or c.fixed_amount > 0 or c.fixed_amount_redemption > 0:
            valid += 1
    coverage = (valid / total) if total > 0 else 0.0
    if coverage < 0.5:
        reasons.append(f"成分行情覆盖率 {coverage*100:.0f}% <50%，自算IOPV不可信")

    # 参照优先官方IOPV(盘中)；休市/取不到时用 navPerCU/最小申赎单位(T-1净值基准)兜底
    ref = official_iopv if (official_iopv and official_iopv > 0) else (
        (info.nav_per_cu / unit) if (info.nav_per_cu > 0 and unit > 0) else 0.0)
    ref_src = "官方" if (official_iopv and official_iopv > 0) else "净值基准"
    if ref > 0 and iopv_mid > 0:
        err_bps = (iopv_mid - ref) / ref * 10000.0
        dev = abs(iopv_mid - ref) / ref
        dev_ok = dev <= 0.3
    else:
        err_bps = 0.0
        dev = 0.0
        dev_ok = True   # 无任何参照时，不因偏离判不可信（只看覆盖率）
    trustworthy = (coverage >= 0.5) and dev_ok
    if not dev_ok:
        reasons.append(f"自算IOPV({iopv_mid:.4f})偏离{ref_src}({ref:.4f})达{dev*100:.0f}%，不可信")
    # 不可信时 premium 用参照兜底（看板溢价列有意义）
    if not trustworthy and ref > 0:
        prem = premium_rate(etf_price, ref)

    ratio = cash_replace_ratio(info, quotes)
    if ratio >= 0.05:
        reasons.append(f"现金替代占比 {ratio*100:.1f}% ≥5%，IOPV 偏差风险大")
    if basket_c <= 0 or basket_r <= 0:
        reasons.append("篮子估值不全(缺少行情/停牌)，结果不可信")

    # ---- 动态现金差额偏差（隐藏滑点池，帖子主要亏损源）----
    bias, bias_bps = cash_diff_bias(info, quotes)
    if abs(bias_bps) >= 5.0:
        reasons.append(f"现金差额动态偏差 {bias_bps:+.1f}bp(约{bias:+,.0f}元)——"
                       f"按当前价收盘实际差额将{'少' if bias > 0 else '多'}于预估，"
                       f"{'折价赎回' if bias > 0 else '溢价申购'}方向会被{'多扣' if bias > 0 else '多补'}")
    # 动态风险预留：偏差大时按实际偏差扣（不小于静态预留的1/2，避免过于乐观）
    dynamic_cash_risk_bps = max(abs(bias_bps), cost.cash_diff_risk_bps / 2.0)

    # ---- 溢价套利（申购）----
    pnl_premium = (etf_notional - basket_c) \
        - cost.stock_buy_fee(basket_c) - cost.etf_sell_fee(etf_notional) \
        - cost.cash_diff_risk(basket_c) - cost.fund_cost(max(basket_c, etf_notional)) \
        - basket_c * dynamic_cash_risk_bps / 10000.0

    # ---- 折价套利（赎回）----
    pnl_discount = (basket_r - etf_notional) \
        - cost.etf_buy_fee(etf_notional) - cost.stock_sell_fee(basket_r) \
        - cost.cash_diff_risk(basket_r) - cost.fund_cost(max(etf_notional, basket_r)) \
        - basket_r * dynamic_cash_risk_bps / 10000.0

    # ---- 费用拆解（供看板展示，与下方 pnl 已扣费用完全一致）----  [AI-2026-08-29]
    fee_etf = cost.etf_buy_fee(etf_notional) + cost.etf_sell_fee(etf_notional)
    fee_basket = cost.stock_buy_fee(basket_c) + cost.stock_sell_fee(basket_r)
    fee_creation = cost.fund_cost(max(basket_c, etf_notional))
    fee_redemption = cost.fund_cost(max(etf_notional, basket_r))

    # ---- 决策：仅当自算 IOPV 可信时才出信号（不可信时 IOPV 仍如实返回供观察）----
    direction = "none"
    pnl = 0.0
    if trustworthy:
        if pnl_premium > 0 and pnl_premium >= pnl_discount:
            direction = "premium"
            pnl = pnl_premium
        elif pnl_discount > 0:
            direction = "discount"
            pnl = pnl_discount

    denom = max(basket_c, basket_r, 1.0)
    return ArbSignal(
        direction=direction,
        etf_price=etf_price,
        iopv_mid=round(iopv_mid, 4),
        iopv_creation=round(iopv_c, 4),
        iopv_redemption=round(iopv_r, 4),
        premium=round(prem * 10000.0, 1),          # 单位：bps
        pnl_per_unit=round(pnl, 2),
        pnl_bps=round(pnl / denom * 10000.0, 1),
        cash_replace_ratio=round(ratio, 4),
        reasons=reasons,
        cash_bias=bias,
        cash_bias_bps=bias_bps,
        official_iopv=round(official_iopv, 4) if official_iopv else 0.0,
        iopv_err_bps=round(err_bps, 1),
        trustworthy=trustworthy,
        fee_etf=round(fee_etf, 2),
        fee_basket=round(fee_basket, 2),
        fee_creation=round(fee_creation, 2),
        fee_redemption=round(fee_redemption, 2),
    )


# ---------------------------------------------------------------------------
# 便捷展示
# ---------------------------------------------------------------------------

def format_signal(sig: ArbSignal) -> str:
    """把评估结果格式化成一行可读文本（用于监控日志）。"""
    d = {"premium": "溢价→申购套利", "discount": "折价→赎回套利", "none": "无信号"}[sig.direction]
    bias_txt = f" 差额偏差{sig.cash_bias_bps:+.1f}bp" if sig.cash_bias_bps else ""
    return (
        f"[{d}] 市价{sig.etf_price:.3f} IOPV(申){sig.iopv_creation:.4f}/"
        f"IOPV(赎){sig.iopv_redemption:.4f}/中{sig.iopv_mid:.4f} "
        f"溢价{sig.premium:+.1f}bp 净盈亏{sig.pnl_bps:+.1f}bp/篮子({sig.pnl_per_unit:+.0f}元)"
        f"{bias_txt} 现金替代占{sig.cash_replace_ratio*100:.1f}%"
    )
