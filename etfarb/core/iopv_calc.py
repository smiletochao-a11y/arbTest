#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/iopv_calc.py — P3: IOPV 计算 + 校准引擎
====================================================================================
数据口径（2026-09-15 三重实测钉死，详见 docs/21）：
  官方 PCF(T) 的「前一日」段给出 D=T-1 的净资产：
      NAVperCU(D)  =  Σ替代金额(D)            +  预估现金(T)
                        ↑ 官方篮子(股票市值)      ↑ 该篮子所属公告 T 的预估现金
  而 Σ替代金额(D) = Σ( 数量 × 参考价(D) × 中间价(D) )
      参考价(D) = 不复权收盘(D) − Σ{ 股息(E) : D < 除权日 E ≤ T }   ← 见 hk_quote.get_reference_closes

  ⇒ 盘中 IOPV = ( Σ(数量 × 实时HKD价 × 实时汇率) + 预估现金(T) ) ÷ 申赎单位
     （实时价取自 qt.gtimg.cn，本身即真实成交价，不受复权影响）
  ⇒ 收盘 IOPV(T) = ( Σ(数量(T公告) × T日不复权收盘 × T日中间价) + 预估现金(T公告) ) ÷ 申赎单位
     键 = 公告日 T，落 iopv_close；篮子与预估现金同源，与盘中口径一致（收盘时刻两值相等）
     🔴 只对「已收盘」的交易日计算（公告日 < 今天）—— 腾讯日K盘中就有当日K线，
        不拦会把盘中价当收盘价落库且永久 OK。见 IOPVCloseCalc.calc_all。

⚠️ 两个市场的 PCF 差异（2026-09-16 起同时支持）：
  · 虚拟行 `159900 申赎现金`：深交所 PCF 才有，**不是持仓**（shares=0），
    只承载「官方篮子货值」，必须从 IOPV 求和中排除（见 _load_components）。
  · 官方篮子货值：上交所分摊在每只成分的 `SubstitutionCashAmount`；
    深交所整笔记在 159900 行（且含 15% 保证金，落库时已还原为货值）。
    两边都满足恒等式 `NAVperCU = 官方篮子货值 + 预估现金(T)`。
  · **停牌券**：当日无成交 ⇒ 无日K ⇒ 按「前收盘」(≤D 的最后一根不复权收盘) 估值，
    并在 iopv_verify.note 里列明 `code@取价日`、状态降为 WARN（复牌跳空会背离实际成本）。

⚠️ 红线（东哥 2026-09-14 明确）：**绝不兜底、绝不抄替代金额列、缺数据必暴露错误**。
  · 汇率缺失 → FXMissingError（fx_provider 严格模式，不用 config 默认值）
  · 成分股行情缺失（实时/历史）→ IOPVDataMissing
  · PCF 缺失 → IOPVDataMissing
  任何一项缺失都直接抛异常，让上游（面板/告警）显示错误，而不是静默算出一个错的 IOPV。

⚠️ 净值口径（2026-09-15 定）：**净值一律直接取官方 NAVperCU / NAV 字段，绝不倒算**。
  PCF 篮子只是「名义篮子」，与基金实际持仓存在日间微差（实测 0.01%~0.03%），
  用篮子+cash 反推净值没有意义。本模块只输出官方净值字段 + 篮子校准结果。

类：
  IOPVCalculator : calc_realtime / calc_basket_for_basket_date / calc_close_iopv_for_announcement / persist_point
  IOPVVerifier   : verify_basket_date / verify_all_pending   （结果落 iopv_verify，键=篮子基准日 D）
  IOPVCloseCalc  : calc_one / calc_all                       （结果落 iopv_close，键=公告日 T）

用法：
  python iopv_calc.py            # 自检：校准全部待校准篮子 + 盘中实时 IOPV
"""
import os
import re
import ssl
import sqlite3
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import db as _db   # noqa: E402  core/db.py —— 单库方案 arb_master.db
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def cash_value_of(comp):
    """成分的「必须现金替代」货值（CNY，D 日篮子口径）；无则 None。

    🔴 深市 PCF 里这类成分 `股票数量`=0、货值写在『申购替代金额』列，落库在
       `pcf_component.cash_value`。**它不是持仓、但属于篮子货值** —— 漏了它 IOPV
       会整块偏低（docs/40 §七·15）。上交所 XML 无此类行（恒为 None）。"""
    v = comp.get("cash_value")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None

from fx_provider import FXProvider, FXMissingError
from hk_quote import HKQuoteFetcher
from index_proxy import IndexProxyQuote, IndexProxyMissing

# 校准健康度阈值（bps）：实测正常篮子误差 < 0.001bp；
# 复权/股息漏算约 2~16bp；≥50bp 说明数据源或口径出了真问题。
ERR_OK_BPS = 5.0
ERR_WARN_BPS = 50.0


class IOPVDataMissing(Exception):
    """IOPV 计算所需数据（PCF/行情）缺失时抛出。绝不静默兜底。"""
    pass


def _td8_to_iso(td):
    td = str(td)
    return f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td) == 8 and td.isdigit() else td


class IOPVCalculator:
    def __init__(self, fund_code, db=DB_PATH, hk=None):
        self.db = db
        self.fund_code = fund_code
        self.cfg = self._load_config()
        self.units = self.cfg["creation_redemption_unit"]
        self.fx = FXProvider(fund_code, db)
        # `hk` 可由调用方**注入**（2026-09-17）：HKQuoteFetcher 是**有状态**的
        # （内存日K缓存 `_raw_cache` / 除息缓存 `_exdiv_cache`），批量场景（看板 246 只）
        # 必须全批复用同一个实例才有预热效果；缺省 None ⇒ 自己建一个。
        self.hk = hk if hk is not None else HKQuoteFetcher()
        # 「单券 FoF」指数代理通道（quote_source='index_proxy'，2026-09-16 为 513000 立）
        self.proxy = IndexProxyQuote()
        self.index_key = (self.cfg.get("index_key") or "").strip() or None

    # ---- 配置 / 数据加载 ----
    def _load_config(self):
        c = sqlite3.connect(self.db)
        cols = [d[1] for d in c.execute("PRAGMA table_info(fund_iopv_config)")]
        row = c.execute("SELECT * FROM fund_iopv_config WHERE fund_code=?",
                        (self.fund_code,)).fetchone()
        c.close()
        if not row:
            raise IOPVDataMissing(f"[{self.fund_code}] fund_iopv_config 无配置")
        return dict(zip(cols, row))

    def _latest_pcf_date(self):
        c = sqlite3.connect(self.db)
        r = c.execute("SELECT MAX(trade_date) FROM pcf_snapshot WHERE fund_code=?",
                     (self.fund_code,)).fetchone()
        c.close()
        if not r or not r[0]:
            raise IOPVDataMissing(f"[{self.fund_code}] pcf_snapshot 无任何记录")
        return r[0]

    def _load_components(self, announce_td):
        """按「公告日」(TradingDay) 取该份 PCF 的头信息 + 成分篮子。
        注意：成分价格基准是 pre_trade_date，不是 announce_td。

        🔴 排除虚拟行 `159900 申赎现金`（深交所 PCF 才有；上交所 XML 无此券）：
          它不是持仓，只承载「官方篮子货值」等现金替代信息，
          shares=0，但 `codes` 里一旦带上它，`hk159900` 取不到价 →
          整只基金的实时 IOPV 直接抛 IOPVDataMissing（假故障）。

        🔴 `cash_value`（2026-09-17 加，修 docs/40 §七·15）：深市「必须现金替代」成分
          `shares`=0，货值单独存这一列。**它不是持仓的一部分，但属于篮子货值的一部分**
          —— 漏了它，IOPV 会整块偏低（159960 实测 −767bp）。取价见 `_subst_effective()`。"""
        c = sqlite3.connect(self.db)
        rows = c.execute(
            "SELECT stock_code, shares, subst_amount, cash_value FROM pcf_component "
            "WHERE fund_code=? AND trade_date=? AND stock_code<>'159900'",
            (self.fund_code, announce_td)).fetchall()
        snap_all = c.execute(
            "SELECT trade_date, pre_trade_date, nav, cash_diff, est_cash_part, unit_nav, units "
            "FROM pcf_snapshot WHERE fund_code=? AND trade_date=?",
            (self.fund_code, announce_td)).fetchone()
        off = c.execute(
            "SELECT COALESCE(SUM(subst_amount),0) FROM pcf_component "
            "WHERE fund_code=? AND trade_date=?", (self.fund_code, announce_td)).fetchone()
        c.close()
        if not snap_all:
            raise IOPVDataMissing(f"[{self.fund_code}] {announce_td} 无 PCF 头信息")
        if not rows:
            raise IOPVDataMissing(f"[{self.fund_code}] {announce_td} 无 PCF 成分数据")
        snap = snap_all
        comps = [{"code": r[0], "shares": r[1], "subst": r[2], "cash_value": r[3]} for r in rows]
        return {
            "announce_td": snap[0], "basket_td": snap[1], "nav": snap[2],
            "cash_diff": snap[3], "est_cash_part": snap[4], "unit_nav": snap[5],
            "units": snap[6] or self.units, "comps": comps,
            # 官方篮子货值：= 全部成分 subst_amount 之和（含虚拟行；
            # 深交所把货值记在 159900 行，上交所分摊在每只股票上，两者口径一致）
            "official_basket_sum": float(off[0] or 0.0) if off else None,
        }

    def _subst_effective(self, comps, fx, d_close=None):
        """把「必须现金替代」成分的 D 日货值折成**等效股数**，并入逐券篮子。

        🔴 原理（修 docs/40 §七·15）：官方「申购替代金额」= 该成分在一个篮子里的
           **人民币货值**，按 **D 日（= pre_trade_date）收盘价 × D 日汇率** 折算。
           因此等效股数 = 货值 ÷ (D 价 × D 汇率)；实时货值 = 等效股数 × 实时价 × 实时汇率
           = 货值 × (实时价 ÷ D 价) × (实时汇率 ÷ D 汇率) —— 与逐券路径**同构**。

        · `fx`：实时汇率（用于把 D 货值折成股数）。汇率日内漂移 <0.1%，对货值占比
          7.6% 的标的折算误差 <0.8bp，实测 159960 用实时汇率反推得 **+1.5bp**（官方口径）。
        · `d_close`：`{code: D 日**未调整**收盘价}`。官方替代金额就是按未调整收盘算的，
          **不能**用 `get_reference_closes()` 的除息调整价（有分红的券会偏）。
        返回 (comps', missing) —— comps' 中这些行的 `shares` 被替换为等效股数（浮点），
        并打 `is_subst=True`；缺少 D 价/汇率的行原样留在 missing 里，由调用方决定。"""
        if not any(cash_value_of(x) for x in comps):
            return comps, []
        d_close = d_close or {}
        missing = []
        out = []
        for x in comps:
            cv = cash_value_of(x)
            if cv is None:
                out.append(x)
                continue
            px = d_close.get(x["code"])
            if not px or not fx:
                missing.append(x["code"])
                out.append(x)
                continue
            y = dict(x)
            y["shares"] = cv / (float(px) * fx)     # 等效股数（浮点，仅估值用）
            y["is_subst"] = True
            out.append(y)
        return out, missing

    def find_announcement_for_basket(self, basket_td):
        """给定篮子基准日 D，找「下一份公告」(pre_trade_date=D) 的 TradingDay。"""
        c = sqlite3.connect(self.db)
        r = c.execute(
            "SELECT trade_date FROM pcf_snapshot WHERE fund_code=? AND pre_trade_date=? "
            "ORDER BY trade_date DESC LIMIT 1", (self.fund_code, str(basket_td))).fetchone()
        c.close()
        if not r:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {basket_td} 篮子所属公告尚未发布（需 {basket_td} 的下一份 PCF）")
        return r[0]

    @staticmethod
    def _td_to_iso(trade_date):
        return _td8_to_iso(trade_date)

    def get_etf_market_price(self, fund_code):
        """A股 ETF 实时市价（腾讯 qt.gtimg.cn，上交所=sh / 深交所=sz）。取不到即暴露错误。"""
        prefix = "sh" if fund_code[0] in "56" else "sz"
        q = prefix + fund_code
        url = f"https://qt.gtimg.cn/q={q}"
        raw = self.hk._get(url)
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r'v_(sh|sz)(\d+)="(.*)"', line)
            if m and m.group(2) == fund_code:
                f = m.group(3).split("~")
                try:
                    return float(f[3])
                except (IndexError, ValueError):
                    pass
        raise IOPVDataMissing(f"[{fund_code}] 无法取实时市价（腾讯接口无返回）")

    # ---- 实时计算：指数代理通道（单券 FoF，成分无行情源） ----
    def _load_header(self, announce_td):
        """只取 PCF **头部**（不要求成分）——单券 FoF 走指数代理时篮子不由成分推。"""
        c = sqlite3.connect(self.db)
        row = c.execute(
            "SELECT trade_date, pre_trade_date, nav, cash_diff, est_cash_part, unit_nav, units "
            "FROM pcf_snapshot WHERE fund_code=? AND trade_date=?",
            (self.fund_code, announce_td)).fetchone()
        c.close()
        if not row:
            raise IOPVDataMissing(f"[{self.fund_code}] {announce_td} 无 PCF 头信息")
        return {"announce_td": row[0], "basket_td": row[1], "nav": row[2],
                "cash_diff": row[3], "est_cash_part": row[4], "unit_nav": row[5],
                "units": row[6] or self.units}

    def calc_realtime_index_proxy(self, announce_td=None, scaler=None, market_price=None):
        """🔴 指数代理法（2026-09-16 为 513000 立；原理/前提/残差见 core/index_proxy.py）。

            IOPV(T) = [ 篮子货值(D) × 指数比(T/D) × 汇率比(T/D) + 预估现金(T) ] ÷ 申赎单位

        · `篮子货值(D) = NAVperCU(D) − PreCashComponent(D)` —— 单券全现金替代时与官方
          `Σ SubstitutionCashAmount` 等价（513000 实测 1,084,237.66 分毫一致），
          **因此不需要成分明细**（易方达历史 JSON 通道本就不含成分）。
        · 两个比值取自「现价 + 昨收」的**同一次响应** ⇒ 不需要任何历史接口。
        · ⚠️ 这是**近似取价**：以「跟踪指数」代表「成分自身价格」，残差 = 成分相对指数的
          跟踪误差 + 成分自身折溢价 ⇒ 结果标 `quote_mode='index_proxy'`，不与逐券法混比。
        · `scaler` / `market_price` 可由调用方**注入**（看板批量刷新时先集中取数，
          避免逐只重复请求）；缺省 `None` ⇒ 本方法自己取。**公式始终在本方法内**。
        """
        if announce_td is None:
            announce_td = self._latest_pcf_date()
        info = self._load_header(announce_td)
        if not self.index_key:
            raise IOPVDataMissing(
                f"[{self.fund_code}] quote_source=index_proxy 但 fund_iopv_config.index_key 为空")
        basket_d = (info["unit_nav"] or 0.0) - (info["cash_diff"] or 0.0)
        if not basket_d:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {announce_td} 篮子货值(D) = NAVperCU − PreCashComponent 为 0")
        if scaler is None:
            try:
                sc = self.proxy.get_basket_scaler(self.index_key, self.fx.ccy_pair)
            except IndexProxyMissing as e:
                raise IOPVDataMissing(f"[{self.fund_code}] 指数代理取价失败：{e}")
        else:
            sc = scaler
        est = info["est_cash_part"]
        if est is None:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {announce_td} 公告无预估现金，无法算 IOPV —— 不兜底")
        units = info["units"]
        basket = basket_d * sc["scaler"]
        iopv = (basket + est) / units
        mkt = market_price if market_price is not None else self.get_etf_market_price(self.fund_code)
        premium_bps = (mkt - iopv) / iopv * 10000 if iopv else None
        return {
            "fund_code": self.fund_code, "trade_date": announce_td, "mode": "realtime",
            "quote_mode": "index_proxy",
            "basket": basket, "basket_at_D": basket_d,
            "est_cash_part": est, "basket_td": info["basket_td"],
            "iopv": iopv, "market_price": mkt, "premium_bps": premium_bps,
            "proxy_scaler": sc["scaler"],
            "index_name": sc["index"]["name"], "index_last": sc["index"]["last"],
            "index_prev": sc["index"]["prev"], "index_ratio": sc["index"]["ratio"],
            "fx_ratio": sc["fx"]["ratio"], "fx_last": sc["fx"]["last"],
            "fx_prev": sc["fx"]["prev"], "fx_source": sc["fx"]["source"],
            "n_comps": None,
            "official_unit_nav": info["unit_nav"],
            "official_nav_per_share": info["nav"],
            "premium_threshold_bps": self.cfg["premium_threshold_bps"],
            "discount_threshold_bps": self.cfg["discount_threshold_bps"],
        }

    # ---- 实时计算 ----
    def calc_realtime(self, announce_td=None, marks=None, fx_info=None, market_price=None,
                      d_close=None):
        """盘中实时 IOPV。篮子用最新公告的成分，价格用实时价，汇率用**实时市场汇率**。

        分派：`quote_source='index_proxy'`（单券 FoF、成分无行情源）走指数代理通道；
        其余走逐券港股价通道。

        🔴 汇率口径（2026-09-17 修）：逐券路径用 `FXProvider.get_spot_rate_info()`
        （新浪实时市场汇率），**不是** chinamoney 中间价 —— 后者结构性偏高 +0.81%
        导致溢价虚高 +81bp。详见 docs/21 §5.2。

        🔴 行情注入（2026-09-17 加，为「跨境看板」批量刷新立）：`marks`（港股价+停牌标记）、
        `fx_info`、`market_price` 三者均可由调用方**集中取好后传入**，避免 N 只标的
        各自重复请求（N=246 时每轮近 500 个 HTTP）。缺省 `None` ⇒ 本方法自己取。
        **IOPV 公式始终在本方法内**，注入的只是「取价」这一步，不改任何口径。"""
        if (self.cfg.get("quote_source") or "").strip().lower() == "index_proxy":
            return self.calc_realtime_index_proxy(announce_td, market_price=market_price)
        if announce_td is None:
            announce_td = self._latest_pcf_date()
        info = self._load_components(announce_td)
        comps = info["comps"]
        codes = [x["code"] for x in comps]
        if marks is None:
            marks = self.hk.get_realtime_marks(codes)
        missing = [c for c in codes if c not in marks]
        if missing:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {announce_td} 实时价缺失 {len(missing)}/{len(codes)} 只: "
                f"{missing[:10]}{'...' if len(missing) > 10 else ''} —— 不兜底，无法算 IOPV")
        rt = {c: v[0] for c, v in marks.items()}
        # 🔴 汇率口径（2026-09-17 修，docs/21 §5.2 / docs/40 §七·1）：
        #   逐券 IOPV 用**实时市场汇率**（新浪 fx_shkdcny 现价）—— 与官方实时 IOPV
        #   的隐含汇率同源（实测 0.855183 vs 官方隐含 0.85509）。
        #   原用 chinamoney **中间价**（0.8621）会因 USD/HKD 7.80 的折算基准而系统性
        #   偏高 **+0.81%** ⇒ IOPV 偏低、**溢价虚高 +81bp**（513590 实测 +82.0bp、
        #   159366 +83.4bp）。取不到即抛 FXMissingError —— **绝不退回中间价**。
        #   ⚠️ 指数代理通道不受影响（用汇率**比**，口径自动约掉）。
        if fx_info is None:
            fx_info = self.fx.get_spot_rate_info()
        fx = fx_info["rate"]
        # 🔴 「必须现金替代」成分（shares=0、货值落在 cash_value）先折成**等效股数**并入篮子。
        #    2026-09-17 修 docs/40 §七·15：漏了它们 IOPV 会整块偏低（159960 −767bp ⇒ 修复后 +1.5bp）。
        #    D 价必须取 **basket_td 当日的未调整收盘**（官方替代金额的基准），
        #    不能用除息调整价，也不能拿实时口的「昨收」代 —— 历史回补时两者不同日。
        if any(cash_value_of(x) for x in comps):
            need = [x["code"] for x in comps if cash_value_of(x)]
            d_iso = _td8_to_iso(info["basket_td"]) if info.get("basket_td") else None
            dc = d_close            # 允许调用方注入（看板批量取数后分发，省掉逐只重复请求）
            if dc is None:
                dc = {}
                if d_iso:
                    dc = {k: (v or {}).get(d_iso)
                          for k, v in self.hk.get_raw_closes(need, d_iso, d_iso).items()}
            comps, subst_missing = self._subst_effective(comps, fx, dc)
            if subst_missing:
                raise IOPVDataMissing(
                    f"[{self.fund_code}] 现金替代成分缺 D 日({d_iso})未调整收盘 "
                    f"{len(subst_missing)}/{len(need)} 只: {subst_missing[:10]} —— 不兜底")
        est_cash = info["est_cash_part"] or 0.0
        basket = sum(x["shares"] * rt[x["code"]] * fx for x in comps)
        iopv = (basket + est_cash) / info["units"]
        mkt = market_price if market_price is not None else self.get_etf_market_price(self.fund_code)
        premium_bps = (mkt - iopv) / iopv * 10000 if iopv else None
        # 🔴 停牌成分（2026-09-17 立）：停牌券的实时现价 = 停牌前收盘价，**照算不拦**
        #   （这正是交易所 IOPV 口径：没有「最新成交价」就沿用停牌前收盘价，见 docs/21 §二·F）。
        #   但要把这件事**暴露给上层**去标「停牌中 · 暂停参考」：停牌期间申赎替代款要等
        #   复牌后按实际成本结算，折溢价数字此时没有兑现意义。
        susp_codes = sorted(c for c in codes if marks[c][1])
        hk_val = sum(x["shares"] * rt[x["code"]] for x in comps)          # 港元口径
        susp_hk = sum(x["shares"] * rt[x["code"]] for x in comps if marks[x["code"]][1])
        return {
            "fund_code": self.fund_code, "trade_date": announce_td, "mode": "realtime",
            "basket": basket, "est_cash_part": info["est_cash_part"],
            "basket_td": info["basket_td"],
            "iopv": iopv,
            "market_price": mkt, "premium_bps": premium_bps,
            "fx": fx, "fx_source": fx_info["source"], "fx_date": fx_info["rate_date"],
            "n_comps": len(comps),
            "suspended_codes": susp_codes,
            "suspended_weight": (susp_hk / hk_val) if hk_val else None,
            "official_unit_nav": info["unit_nav"],
            "official_nav_per_share": info["nav"],
            "premium_threshold_bps": self.cfg["premium_threshold_bps"],
            "discount_threshold_bps": self.cfg["discount_threshold_bps"],
        }

    # ---- 指数代理：篮子「校准」与「T 日收盘 IOPV」（单券 FoF 专用） ----
    def _basket_calib_index_proxy(self, basket_td):
        """单券 FoF 的篮子校准：篮子货值**直接取官方锚点**。

        `篮子货值(D) = NAVperCU(D) − PreCashComponent(D)` —— 与官方
        `Σ SubstitutionCashAmount` 等价（513000 实测分毫一致），**不存在取价环节**，
        故误差恒为 0：这是**口径**，不是巧合。

        ⇒ 对这类标的，「校准」验证的是**锚点恒等式**（PCF 内自洽），而不是
          「我们算的价能否复现官方」。返回 `method='official_anchor'` 供上层区分。
        """
        ann = self.find_announcement_for_basket(basket_td)
        info = self._load_header(ann)
        anchor = (info["unit_nav"] or 0.0) - (info["cash_diff"] or 0.0)
        return {
            "fund_code": self.fund_code, "mode": "calibration",
            "method": "official_anchor",
            "basket_td": str(basket_td), "announce_td": ann,
            "computed_basket": anchor, "official_basket": anchor, "error_bps": 0.0,
            "fx": None, "fx_source": "官方锚点（无取价环节）",
            "n_comps": None, "drops": {}, "unparsed": [], "suspended": {},
            "official_unit_nav": info["unit_nav"], "official_nav": info["nav"],
            "est_cash_part": info["est_cash_part"], "cash_diff": info["cash_diff"],
            "units": info["units"],
        }

    def _close_iopv_index_proxy(self, announce_td):
        """单券 FoF 的「T 日收盘 IOPV」。

        篮子货值(T) 取自**下一期 PCF**（其 PreTradingDay = T）—— D=T 的官方篮子正是
        按 T 日收盘价估的：

            IOPV(T收盘) = [ 篮子货值(T) + 预估现金(T) ] ÷ 申赎单位

        ⇒ 对单券全现金替代标的，本式**恒等于下一期公告的官方净值 NAV**（可交叉校验）。
        """
        nxt = self.find_announcement_for_basket(announce_td)
        nb = self._load_header(nxt)
        info = self._load_header(announce_td)
        est = info["est_cash_part"]
        if est is None:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {announce_td} 公告无预估现金，无法算收盘 IOPV —— 不兜底")
        basket = (nb["unit_nav"] or 0.0) - (nb["cash_diff"] or 0.0)
        units = info["units"]
        return {
            "fund_code": self.fund_code, "mode": "close",
            "method": "index_proxy_next_pcf",
            "announce_td": str(announce_td), "trade_date": str(announce_td),
            "basket": basket, "est_cash_part": est, "units": units,
            "iopv": (basket + est) / units,
            "mid_fx": None, "fx_source": "官方锚点（无取价环节）",
            "n_comps": None,
            # 单券 FoF 走官方锚点，没有「成分取价」环节 ⇒ 不存在停牌回退
            "suspended": {},
            "official_unit_nav": info["unit_nav"], "official_nav": info["nav"],
        }

    # ---- 历史篮子校准计算 ----
    def calc_basket_for_basket_date(self, basket_td):
        """按篮子基准日 D 现算官方篮子市值，与 PCF 的官方篮子货值 对照。

        口径：参考价(D) = 不复权收盘(D) − Σ{股息(E) : D < E <= T}，T = 该篮子所属公告日。
        **停牌券**（当日无成交 → 无日K）按「前收盘」(≤D 的最后一根不复权收盘) 估值 ——
        这是交易所对停牌证券的估值惯例，不是兜底造数；但结果里会明确列出
        `suspended {code: 取价日}`，由上层标 WARN（复牌跳空时篮子会背离实际补券成本）。

        返回 dict（含 computed_basket / official_basket / error_bps / fx / drops /
        unparsed / suspended）。
        """
        if (self.cfg.get("quote_source") or "").strip().lower() == "index_proxy":
            return self._basket_calib_index_proxy(basket_td)
        announce_td = self.find_announcement_for_basket(basket_td)
        info = self._load_components(announce_td)
        comps = info["comps"]
        # 🔴 「必须现金替代」成分（shares=0）不参与逐券取价：官方替代金额本身就是它的
        #    **人民币货值**（按 D 日未调整收盘 × D 日汇率折出），直接原值计入 computed
        #    —— 与 official_basket_sum（159900 的整篮子货值）同口径（docs/40 §七·15）。
        subst_comps = [x for x in comps if cash_value_of(x)]
        priced = [x for x in comps if not cash_value_of(x)]
        codes = [x["code"] for x in priced]
        d_iso, t_iso = _td8_to_iso(basket_td), _td8_to_iso(announce_td)
        ref, raw, drops, unparsed = self.hk.get_reference_closes(codes, d_iso, t_iso)
        suspended = {}
        missing = [c for c in codes if c not in ref]
        if missing:
            filled, susp = self.hk.get_reference_closes_asof(missing, d_iso, t_iso)
            ref.update(filled)
            suspended.update(susp)
            still = [c for c in codes if c not in ref]
            if still:
                raise IOPVDataMissing(
                    f"[{self.fund_code}] {basket_td} 参考价缺失 {len(still)}/{len(codes)} 只: "
                    f"{still[:10]}{'...' if len(still) > 10 else ''} —— 不兜底")
        fx_info = self.fx.get_rate_info(basket_td)   # 严格：无则 FXMissingError
        fx = fx_info["rate"]
        computed = (sum(x["shares"] * ref[x["code"]] * fx for x in priced)
                    + sum(cash_value_of(x) for x in subst_comps))
        # 官方篮子货值 = pcf_component 全表 subst_amount 之和（含虚拟行 159900）
        official = info.get("official_basket_sum")
        if official is None:
            official = sum(x["subst"] for x in comps if x["subst"] is not None)
        error_bps = (computed - official) / official * 10000 if official else None
        return {
            "fund_code": self.fund_code, "mode": "calibration",
            "basket_td": str(basket_td), "announce_td": announce_td,
            "computed_basket": computed, "official_basket": official,
            "error_bps": error_bps, "fx": fx, "fx_source": fx_info["source"],
            "n_comps": len(comps), "drops": drops, "unparsed": unparsed,
            "suspended": suspended,
            "official_unit_nav": info["unit_nav"], "official_nav": info["nav"],
            "est_cash_part": info["est_cash_part"], "cash_diff": info["cash_diff"],
            "units": info["units"],
        }

    def calc_close_iopv_for_announcement(self, announce_td):
        """计算「公告 T」当日收盘时点的 IOPV（东哥 2026-09-15 定的口径）。

            IOPV(T收盘) = [ Σ(T公告成分股数 × T日港股不复权收盘 × T日中间价) + T公告预估现金 ] ÷ 申赎单位

        与 calc_basket_for_basket_date 的关键区别：
          · 这里**用 T 公告自己的成分**、按 **T 日**价格估值 → 得到「T 日收盘的 IOPV」；
            那份方法是用「下一份公告」的成分、按 D 日价格估值 → 只能复现官方 Σ替代金额（校准用）。
          · 价格用 **T 日不复权收盘**（真实成交价 = 当日市值），不做股息调整 ——
            参考价规则里的区间 (D, T] 在 D=T 时为空，故 ref(T) ≡ 不复权收盘(T)。
          · 篮子与预估现金**同源**（都是 T 公告），不存在跨日拼接；
            与主看板实时 IOPV 同口径（收盘时刻两值相等）。
          · **停牌成分**：无当日成交 ⇒ 按 ≤T 最后一根不复权收盘估值（= 交易所 IOPV
            口径，与校准路径 `calc_basket_for_basket_date` 同处理），**照算不拦**，
            结果带 `suspended {code: 取价日}`，由上层落 `status=WARN`（2026-09-17 东哥定）。
        """
        if (self.cfg.get("quote_source") or "").strip().lower() == "index_proxy":
            return self._close_iopv_index_proxy(announce_td)
        info = self._load_components(announce_td)
        comps = info["comps"]
        codes = [x["code"] for x in comps]
        t_iso = _td8_to_iso(announce_td)
        raw = self.hk.get_raw_closes(codes, t_iso, t_iso)
        missing = [c for c in codes if t_iso not in raw.get(c, {})]
        suspended = {}
        if missing:
            # 🔴 停牌券按「≤T 的最后一根不复权收盘」估值 —— **官方口径**，不是兜底造数：
            #   交易所 IOPV 公式要「最新成交价」，停牌券没有成交 ⇒ 沿用停牌前收盘价
            #   （正式基金净值才另行走估值调整规则）。与校准路径
            #   `calc_basket_for_basket_date` **同一处理**，两处口径就此统一（2026-09-17 东哥定）。
            #   `(t_iso, t_iso]` 区间为空 ⇒ 这里取到的就是「停牌前收盘」原值，不做股息调整。
            #   用了前收盘必须暴露 ⇒ 上层标 `status=WARN` + `note` 列明 `code@取价日`。
            filled, susp = self.hk.get_reference_closes_asof(missing, t_iso, t_iso)
            for c, v in filled.items():
                raw.setdefault(c, {})[t_iso] = v
            suspended.update(susp)
            still = [c for c in codes if t_iso not in raw.get(c, {})]
            if still:
                raise IOPVDataMissing(
                    f"[{self.fund_code}] {announce_td} 收盘价缺失 {len(still)}/{len(codes)} 只: "
                    f"{still[:10]}{'...' if len(still) > 10 else ''} —— 不兜底")
        fx_info = self.fx.get_rate_info(announce_td)   # 严格：无则 FXMissingError
        fx = fx_info["rate"]
        est = info["est_cash_part"]
        if est is None:
            raise IOPVDataMissing(
                f"[{self.fund_code}] {announce_td} 公告无预估现金差额，无法算收盘 IOPV —— 不兜底")
        units = info["units"]
        # 🔴 现金替代成分（shares=0、货值在 cash_value）折等效股数后并入 T 日篮子，
        #    与 `calc_realtime` 同一处理（2026-09-17 修 docs/40 §七·15）。
        if any(cash_value_of(x) for x in comps):
            need = [x["code"] for x in comps if cash_value_of(x)]
            d_iso = _td8_to_iso(info["basket_td"]) if info.get("basket_td") else None
            d_close = {}
            if d_iso:
                d_close = {k: (v or {}).get(d_iso)
                           for k, v in self.hk.get_raw_closes(need, d_iso, d_iso).items()}
            comps, subst_missing = self._subst_effective(comps, fx, d_close)
            if subst_missing:
                raise IOPVDataMissing(
                    f"[{self.fund_code}] 现金替代成分缺 D 日({d_iso})未调整收盘 "
                    f"{len(subst_missing)}/{len(need)} 只: {subst_missing[:10]} —— 不兜底")
        basket = sum(x["shares"] * raw[x["code"]][t_iso] * fx for x in comps)
        return {
            "fund_code": self.fund_code, "mode": "close",
            "announce_td": str(announce_td), "trade_date": str(announce_td),
            "basket": basket, "est_cash_part": est, "units": units,
            "iopv": (basket + est) / units,
            "mid_fx": fx, "fx_source": fx_info["source"],
            "n_comps": len(comps),
            "suspended": suspended,
            "official_unit_nav": info["unit_nav"], "official_nav": info["nav"],
        }

    def persist_point(self, res):
        """实时 IOPV 采样落 iopv_point。"""
        c = sqlite3.connect(self.db)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""INSERT INTO iopv_point
            (fund_code, ts, trade_date, iopv, market_price, premium_bps, source)
            VALUES (?,?,?,?,?,?,?)""",
            (res["fund_code"], ts, res["trade_date"], res["iopv"],
             res["market_price"], res["premium_bps"], "calc_realtime"))
        c.commit()
        c.close()


class IOPVVerifier:
    """把「篮子基准日 D」的现算篮子 与 PCF 官方 Σ替代金额 对照，结果落 iopv_verify。

    iopv_verify.trade_date = **篮子基准日 D**（不是公告日），与历史页行一一对应。
    """

    def __init__(self, fund_code, db=DB_PATH):
        self.fund_code = fund_code
        self.db = db
        self.calc = IOPVCalculator(fund_code, db)
        self._ensure_note_column()

    def _ensure_note_column(self):
        try:
            c = sqlite3.connect(self.db)
            cols = [r[1] for r in c.execute("PRAGMA table_info(iopv_verify)")]
            if "note" not in cols:
                c.execute("ALTER TABLE iopv_verify ADD COLUMN note TEXT")
                c.commit()
            c.close()
        except Exception:
            pass

    def verify_basket_date(self, basket_td, persist=True):
        res = self.calc.calc_basket_for_basket_date(basket_td)
        err = res["error_bps"]
        aerr = abs(err) if err is not None else None
        note = ""
        if res["unparsed"]:
            status = "WARN"
            note = "非现金除权未解析: " + ",".join(res["unparsed"])
        elif aerr is None:
            status = "WARN"; note = "官方篮子为空"
        elif aerr <= ERR_OK_BPS:
            status = "OK"
        elif aerr <= ERR_WARN_BPS:
            status = "WARN"; note = "误差偏大，疑数据源或复权口径"
        else:
            status = "ERROR"; note = "误差异常，须核查 PCF/行情/汇率"
        if res.get("suspended"):
            # 停牌券按「前收盘」估值：口径本身正确（交易所对停牌证券的估值惯例），
            # 但复牌跳空会让篮子与实际补券成本背离 ⇒ 明确标出来、**不得当作 OK**。
            # 副作用（有意保留）：WARN 行每天会被 verify_all_pending 重算 —— 正是为了
            # 停牌结束后自动降级回 OK。
            sus = ", ".join("%s@%s" % (c, d) for c, d in sorted(res["suspended"].items()))
            note = ("停牌券按前收盘估值: " + sus + (" | " + note if note else ""))
            if status == "OK":
                status = "WARN"
        if res["drops"]:
            note = (note + " | " if note else "") + "已扣股息: " + \
                   ",".join(f"{k}:{v}" for k, v in res["drops"].items())
        if persist:
            c = sqlite3.connect(self.db)
            c.execute("""INSERT OR REPLACE INTO iopv_verify
                (fund_code, trade_date, computed_basket, official_basket, error_bps, fx_used, status, note)
                VALUES (?,?,?,?,?,?,?,?)""",
                (self.fund_code, str(basket_td), res["computed_basket"], res["official_basket"],
                 res["error_bps"], res["fx"], status, note or None))
            c.commit()
            c.close()
        out = dict(res)
        out["status"] = status
        out["note"] = note
        return out

    # 兼容旧名（外部若有调用）：语义已是「篮子基准日」
    verify_trade_date = verify_basket_date

    def verify_all_pending(self, persist=True, force=False):
        """对所有「有下一份公告」的篮子基准日跑校准。

        force=False（默认）：已 OK 的跳过（幂等，日常调用用这个）。
        force=True：全部重算（口径/数据源变更后必须用这个，否则旧 OK 行会把新口径挡住）。

        无成分 / 无汇率 → 收集为 skipped，绝不兜底造数。"""
        c = sqlite3.connect(self.db)
        basket_dates = [r[0] for r in c.execute(
            "SELECT DISTINCT pre_trade_date FROM pcf_snapshot "
            "WHERE fund_code=? AND pre_trade_date IS NOT NULL ORDER BY pre_trade_date",
            (self.fund_code,)).fetchall()]
        done = {} if force else {r[0]: r[1] for r in c.execute(
            "SELECT trade_date, status FROM iopv_verify WHERE fund_code=?",
            (self.fund_code,)).fetchall()}
        c.close()
        results = []
        for d in basket_dates:
            if done.get(d) == "OK":
                continue
            try:
                results.append(self.verify_basket_date(d, persist=persist))
            except (IOPVDataMissing, FXMissingError) as e:
                results.append({"basket_td": d, "skipped": str(e)})
        return results


class IOPVCloseCalc:
    """按「公告日 T」计算并落库「T 日收盘 IOPV」（表 iopv_close）。

    历史页行 D 的「收盘IOPV」列取的就是本表 trade_date=D 的 iopv；
    它与主看板实时 IOPV 同口径（收盘时二者相等），D 当天盘后即可算出，
    不必等 D+1 公告（净值/校准列才需要 D+1 公告）。
    """

    def __init__(self, fund_code, db=DB_PATH):
        self.fund_code = fund_code
        self.db = db
        self.calc = IOPVCalculator(fund_code, db)

    def calc_one(self, announce_td, persist=True):
        res = self.calc.calc_close_iopv_for_announcement(announce_td)
        note = None
        status = "OK"
        # 🔴 停牌券 → status=WARN + note 列明 `code@取价日`（2026-09-17 立）：
        #   IOPV **照算照落库**（停牌券按 ≤T 前收盘估值 = 官方口径，见 docs/21 §二·F），
        #   但必须标出来 —— 停牌期间申赎替代款要等复牌后按实际成本结算，这个折溢价
        #   此刻没有兑现意义。前端据此在标的上标「停牌中 · 暂停参考」。
        susp = res.get("suspended") or {}
        if susp:
            status = "WARN"
            items = ", ".join("%s@%s" % (c, d) for c, d in sorted(susp.items())[:6])
            more = "" if len(susp) <= 6 else " 等 %d 只" % len(susp)
            note = ("含停牌券 %d 只，按 ≤T 前收盘估值（官方口径）：%s%s —— "
                    "停牌期间替代款待复牌后按实际成本结算，参考意义有限"
                    % (len(susp), items, more))
        if persist:
            c = sqlite3.connect(self.db)
            c.execute(
                "INSERT OR REPLACE INTO iopv_close "
                "(fund_code, trade_date, basket, est_cash, units, iopv, mid_fx, n_comps, "
                " status, note, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.fund_code, str(announce_td), res["basket"], res["est_cash_part"],
                 res["units"], res["iopv"], res["mid_fx"], res["n_comps"],
                 status, note, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            c.commit()
            c.close()
        out = dict(res)
        out["status"] = status
        out["note"] = note
        return out

    def calc_all(self, persist=True, force=False):
        """对所有「已收盘」的公告日计算收盘 IOPV。

        🔴 只算 `公告日 < 今天`（2026-09-16 加）：腾讯日K在**盘中当天就有当日K线**
        （价格=当前价），若不拦，早上跑校准会把**盘中半天的价**当成"收盘 IOPV"落库，
        且状态直接 OK → 之后永不重算，值就永久错了。
        （实测 9-16 09:06 那次是 50/50 缺价 → 跳过；但改成"抓完新 PCF 立刻校准"后，
          抓取可能发生在盘中，此闸门就不是可选项了。）

        force=False（默认）：已 OK 的跳过（幂等，日常调用用这个）。
        force=True：全部重算（口径变更后用）。
        无成分 / 无收盘价 / 无汇率 / 无预估现金 → 收集为 skipped，绝不兜底造数。"""
        c = sqlite3.connect(self.db)
        anns = [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM pcf_component WHERE fund_code=? "
            "ORDER BY trade_date", (self.fund_code,)).fetchall()]
        done = {} if force else {r[0]: r[1] for r in c.execute(
            "SELECT trade_date, status FROM iopv_close WHERE fund_code=?",
            (self.fund_code,)).fetchall()}
        c.close()
        today8 = datetime.date.today().strftime("%Y%m%d")
        out = []
        for t in anns:
            if done.get(t) == "OK":
                continue
            if str(t) >= today8:
                out.append({"announce_td": t,
                            "skipped": "尚未收盘（公告日 ≥ 今天，收盘价未定）"})
                continue
            try:
                out.append(self.calc_one(t, persist=persist))
            except (IOPVDataMissing, FXMissingError) as e:
                out.append({"announce_td": t, "skipped": str(e)})
        return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="P3: IOPV 计算 + 校准引擎")
    ap.add_argument("--code", default="513590")
    ap.add_argument("--force", action="store_true",
                    help="全部重算校准（口径/数据源变更后用；否则已 OK 的行会被跳过）")
    ap.add_argument("--no-realtime", action="store_true", help="只做校准与收盘 IOPV，不跑盘中实时 IOPV")
    args = ap.parse_args()
    fund = args.code
    print("=" * 72)
    print("P3 自检：IOPVCalculator + IOPVVerifier（%s）" % fund)
    print("=" * 72)

    v = IOPVVerifier(fund)
    print("\n>>> [历史校准] 批量校准篮子（口径：不复权收盘 − 公告日除息）%s"
          % ("[force 全量重算]" if args.force else ""))
    for r in v.verify_all_pending(force=args.force):
        if r.get("skipped"):
            print(f"  {r['basket_td']}  跳过: {r['skipped']}")
            continue
        print(f"  {r['basket_td']} (公告 {r['announce_td']}) 现算 {r['computed_basket']:,.2f} "
              f"vs 官方 {r['official_basket']:,.2f}  误差 {r['error_bps']:+.6f}bp "
              f"[{r['status']}] fx={r['fx']}{('  ' + r['note']) if r['note'] else ''}")

    print("\n>>> [收盘 IOPV] 按公告日 T 计算 T 日收盘时点 IOPV（历史页行 T 的「收盘IOPV」列）")
    cc = IOPVCloseCalc(fund)
    for r in cc.calc_all(force=args.force):
        if r.get("skipped"):
            print(f"  {r['announce_td']}  跳过: {r['skipped']}")
            continue
        print(f"  {r['announce_td']}  篮子 {r['basket']:,.2f} + 预估现金 {r['est_cash_part']:,.2f} "
              f"= {r['basket'] + r['est_cash_part']:,.2f} → IOPV {r['iopv']:.6f} "
              f"(mid={r['mid_fx']}, {r['n_comps']} 只) [{r['status']}]")

    if args.no_realtime:
        return
    print("\n>>> [实时] 盘中 IOPV（依赖盘中港股行情 + A股ETF市价 + 今日FX；"
          "网络受限会暴露错误，符合红线）")
    try:
        calc = IOPVCalculator(fund)
        res = calc.calc_realtime()
        calc.persist_point(res)
        print(f"  公告日 / 篮子基准日        = {res['trade_date']} / {res['basket_td']}")
        print(f"  实时篮子                   = {res['basket']:,.2f}")
        print(f"  盘中 IOPV                  = {res['iopv']:.6f}")
        print(f"  ETF 实时市价               = {res['market_price']:.4f}")
        print(f"  折溢价                     = {res['premium_bps']:+.1f} bps "
              f"({res['premium_bps']/100:+.2f}%)")
        print(f"  官方 NAVperCU / 每份额净值 = {res['official_unit_nav']} / {res['official_nav_per_share']}")
        print(f"  汇率                       = {res['fx']} ({res['fx_source']}, {res['fx_date']})")
        print(f"  → 已落库 iopv_point")
    except (IOPVDataMissing, FXMissingError) as e:
        print(f"  ⚠️ 实时计算暴露错误（符合红线，不兜底）: {e}")


if __name__ == "__main__":
    main()
