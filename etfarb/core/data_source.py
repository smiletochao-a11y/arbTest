# -*- coding: utf-8 -*-
"""
数据源层：把"取申赎清单 + 取行情"抽象成统一接口。

两个实现：
1. MockDataSource   —— 本地模拟数据，不依赖任何终端，用于演示/自检/练手。
2. QMTDataSource    —— 大QMT 内置 API 适配器。注意：只能在银河大QMT 策略编辑器
                       环境内使用（get_etf_info / get_full_tick 等是编辑器内置全局函数，
                       本地普通 Python 里调用会 NameError，属正常现象）。

为什么不用 xtquant：银河大QMT（经典QMT）的策略环境不支持第三方库/xtquant 包，
只能用编辑器内置函数。小QMT(miniQMT)才能 import xtquant。
"""


from typing import Dict, List, Optional

from .iopv_engine import Component, ETFInfo, Quote, ReplaceFlag


class DataSource:
    """统一数据接口。实现者各自处理终端差异。"""

    def get_etf_info(self, code: str) -> Optional[ETFInfo]:
        raise NotImplementedError

    def get_quotes(self, codes: List[str]) -> Dict[str, Quote]:
        raise NotImplementedError

    def get_etf_price(self, code: str) -> float:
        raise NotImplementedError

    def get_official_iopv(self, code: str) -> Optional[float]:
        """官方 IOPV（交易所15秒推送），仅用于与自算值对比。"""
        return None


# ---------------------------------------------------------------------------
# 模拟数据源（本地可跑，不连任何终端）
# ---------------------------------------------------------------------------

class MockDataSource(DataSource):
    """内置一只简化版沪深300样本ETF(510300.SH)和一只跨境中概互联(513050.SH)的模拟数据。

    成分股数量经过压缩（真实篮子几十~几百只，这里只放有代表性的几只），
    并刻意制造了：允许现金替代、必须现金替代(停牌)、涨停、跌停 等场景，
    方便你在本地直接体验引擎如何处理帖子里的各种坑。
    """

    def __init__(self, scenario: str = "normal"):
        self.scenario = scenario
        self._infos = {
            "510300.SH": self._build_510300(),
            "513050.SH": self._build_513050(),
        }
        # 补每篮子净值 nav_per_cu（= Σ成分最新价×数量 + ECC），与基准行情自洽
        base = self._base_quotes()
        for info in self._infos.values():
            stock_value = sum(base[c.code].last * c.volume
                              for c in info.components if c.code in base)
            info.nav_per_cu = stock_value + info.cash_balance

    # ---- 申赎清单 ----
    @staticmethod
    def _build_510300() -> ETFInfo:
        # 最小申赎单位 300,000 份，篮子市值约 100 万元（按成分股量级模拟，未复刻真实权重）
        # 现金替代占比刻意压到 ~7%（接近真实：帖子案例 5.7% 即触发警惕），
        # 保留一只"允许替代"(隆基)和一只"必须替代"(平安银行-跨市场)演示处理逻辑。
        comps = [
            Component("600519.SH", "贵州茅台", 100),
            Component("601318.SH", "中国平安", 5000),
            Component("600036.SH", "招商银行", 5000),
            Component("000858.SZ", "五粮液", 800),
            Component("600030.SH", "中信证券", 4000),
            Component("000333.SZ", "美的集团", 3000),
            Component("601012.SH", "隆基绿能", 2000, ReplaceFlag.ALLOWED, replace_premium=0.10),
            Component("000001.SZ", "平安银行", 3000, ReplaceFlag.MUST, fixed_amount=0.0, is_cross_market=True),
        ]
        return ETFInfo(
            etf_code="510300.SH", etf_name="沪深300ETF(模拟)",
            report_unit=300000, cash_balance=12000.0,
            enable_creation=True, enable_redemption=True, components=comps,
        )

    @staticmethod
    def _build_513050() -> ETFInfo:
        # 跨境ETF：跨市场成分多、必须现金替代占比高（真实场景）
        comps = [
            Component("00700.HK", "腾讯控股", 2000, ReplaceFlag.MUST, fixed_amount=0.0, is_cross_market=True),
            Component("09988.HK", "阿里巴巴-W", 3000, ReplaceFlag.MUST, fixed_amount=0.0, is_cross_market=True),
            Component("03690.HK", "美团-W", 1500, ReplaceFlag.MUST, fixed_amount=0.0, is_cross_market=True),
            Component("600519.SH", "贵州茅台", 100),
            Component("000858.SZ", "五粮液", 100),
        ]
        return ETFInfo(
            etf_code="513050.SH", etf_name="中概互联ETF(模拟)",
            report_unit=1000000, cash_balance=-8000.0,
            enable_creation=True, enable_redemption=True, components=comps,
        )

    # ---- 行情 ----
    def _base_quotes(self) -> Dict[str, Quote]:
        q = {
            "600519.SH": Quote(last=1450.0, bid1=1449.0, ask1=1451.0, prev_close=1450.0),
            "601318.SH": Quote(last=48.5, bid1=48.4, ask1=48.6, prev_close=48.5),
            "600036.SH": Quote(last=36.2, bid1=36.1, ask1=36.3, prev_close=36.2),
            "000858.SZ": Quote(last=132.0, bid1=131.8, ask1=132.2, prev_close=132.0),
            "600030.SH": Quote(last=22.8, bid1=22.7, ask1=22.9, prev_close=22.8),
            "000333.SZ": Quote(last=55.0, bid1=54.9, ask1=55.1, prev_close=55.0),
            "601012.SH": Quote(last=17.5, bid1=17.4, ask1=17.6, prev_close=17.5),
            "000001.SZ": Quote(last=11.2, bid1=11.1, ask1=11.3, prev_close=11.2),   # 必须现金替代
            "00700.HK": Quote(last=380.0, bid1=379.0, ask1=381.0, prev_close=380.0),
            "09988.HK": Quote(last=88.0, bid1=87.5, ask1=88.5, prev_close=88.0),
            "03690.HK": Quote(last=105.0, bid1=104.5, ask1=105.5, prev_close=105.0),
        }
        if self.scenario == "premium":
            # 行情正常，但 ETF 二级价被抬高 → 溢价
            pass
        elif self.scenario == "discount":
            # ETF 二级价被压低 → 折价
            pass
        elif self.scenario == "limit_down":
            # 重仓股跌停：茅台跌停，卖一/买一都在跌停价，成交量小
            q["600519.SH"] = Quote(last=1305.0, bid1=1305.0, ask1=0.0, prev_close=1450.0, limit_down=True)
        elif self.scenario == "halted":
            # 必须现金替代成分停牌：平安银行停牌
            q["000001.SZ"] = Quote(last=11.2, bid1=0.0, ask1=0.0, prev_close=11.2, halted=True)
        return q

    def get_etf_info(self, code: str) -> Optional[ETFInfo]:
        return self._infos.get(code)

    def get_quotes(self, codes: List[str]) -> Dict[str, Quote]:
        base = self._base_quotes()
        return {c: base[c] for c in codes if c in base}

    def get_etf_price(self, code: str) -> float:
        # 基准净值由篮子自动算出（Σ最新价×数量+ECC)/最小申赎单位，保证与篮子自洽，
        # 再按场景制造溢/折价。这模拟的正是"官方口径"——用最新价估的净值。
        q = self._base_quotes()
        info = self._infos[code]
        total = 0.0
        for c in info.components:
            if c.code in q:
                total += q[c.code].last * c.volume
        base = (total + info.cash_balance) / info.report_unit if info.report_unit > 0 else 0.0
        if base <= 0:
            return 0.0
        if self.scenario == "premium":
            return round(base * 1.006, 4)      # 溢价 ~0.6%
        if self.scenario == "discount":
            return round(base * 0.992, 4)      # 折价 ~0.8%
        if self.scenario == "limit_down":
            return round(base * 0.998, 4)      # 重仓跌停、市价微折（假折价陷阱）
        return round(base * 1.001, 4)          # 正常：微小溢价

    def get_official_iopv(self, code: str) -> Optional[float]:
        """模拟"官方IOPV"：故意用最新价+忽略替代溢价，制造与自算值的偏差。"""
        q = self._base_quotes()
        info = self._infos[code]
        total = 0.0
        for c in info.components:
            if c.code in q:
                total += q[c.code].last * c.volume
        return round((total + info.cash_balance) / info.report_unit, 4)


# ---------------------------------------------------------------------------
# 大QMT 内置 API 适配器（只能在 QMT 策略编辑器里用）
# ---------------------------------------------------------------------------

# QMT ReplaceFlag 编码映射（依据银河大QMT 实测数据 2026-08-12）：
#   49 (=ASCII '1')：同市场成分，可实物申赎（physCreateRedeem=1, ReplaceBalance=0）→ 按实物处理，用盘口价
#   50 (=ASCII '2')：跨市场成分，必须现金替代（physCreateRedeem=0, 有 ReplaceBalance/redemptReplaceBalance）
#                    → 用固定替代金额（申购含10%溢价、赎回为基准金额）
# 其余值按禁止现金替代处理。
QMT_FLAG_49 = 49
QMT_FLAG_50 = 50


def parse_qmt_etf_info(raw: dict, code: str) -> Optional[ETFInfo]:
    """把大QMT get_etf_info 的原始返回解析成 ETFInfo（纯函数，本地可测）。

    raw 结构（银河大QMT 实测）：
        { 'etfCode': '510300', 'etfExchID': 'SH', 'reportUnit': 900000,
          'ecc': 109230.48, 'cashBalance': 107403.48, 'nav': 4.7258,
          'enableCreation': 1, 'enableRedemption': 1,
          'creationLimit': 0, 'redemptionLimit': 3600000000,
          'maxCashRatio': 0.5,
          'stocks': { '600025.SH': {'componentCode': '600025', 'componentExchID': 'SH',
                      'componentVolume': 300, 'ReplaceFlag': 49, 'ReplaceRatio': 0.34,
                      'ReplaceBalance': 0.0, 'redemptReplaceBalance': 0.0,
                      'physCreateRedeem': 1, ...}, ... } }
    兼容两种包裹：get_etf_info 可能直接返回该 dict，也可能返回 {时间戳: dict}。
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        return None
    # 兼容 {timetag: info} 包裹：取最新一条
    info = raw
    if set(raw.keys()) and all(_looks_like_timetag(k) for k in raw.keys()) and "stocks" not in raw:
        keys = sorted(raw.keys())
        info = raw[keys[-1]]

    def g(d, *names, default=None):
        for n in names:
            if n in d and d[n] is not None:
                return d[n]
        return default

    # ---- 成分股 ----
    stocks_raw = g(info, "stocks", default={}) or {}
    components: List[Component] = []
    if isinstance(stocks_raw, dict):
        items = list(stocks_raw.items())
    elif isinstance(stocks_raw, list):
        items = [(g(s, "componentCode", "代码", default=""), s) for s in stocks_raw]
    else:
        items = []
    for full_code, s in items:
        if not isinstance(s, dict):
            continue
        code_s = g(s, "componentCode", "代码", default="") or full_code
        if not code_s:
            continue
        exch = g(s, "componentExchID", default="") or (full_code.split(".")[1] if "." in str(full_code) else "")
        flag_raw = g(s, "ReplaceFlag", "现金替代标志", default=49)
        try:
            flag_raw = int(flag_raw)
        except Exception:
            flag_raw = 49
        if flag_raw == QMT_FLAG_50:
            flag = ReplaceFlag.MUST                     # 跨市场必须现金替代
        else:
            flag = ReplaceFlag.FORBIDDEN                # 同市场实物（49）及其他
        comp = Component(
            code=f"{code_s}.{exch}" if exch else str(code_s),
            name=g(s, "componentName", "名称", default=""),
            volume=int(g(s, "componentVolume", "数量", default=0) or 0),
            replace_flag=flag,
            replace_premium=float(g(s, "ReplaceRatio", default=0.10) or 0.10),
            fixed_amount=float(g(s, "ReplaceBalance", default=0.0) or 0.0),
            fixed_amount_redemption=float(g(s, "redemptReplaceBalance", default=0.0) or 0.0),
            is_cross_market=(exch != (g(info, "etfExchID", default="") or "")),
        )
        components.append(comp)

    unit = int(g(info, "reportUnit", "申购赎回单位", default=0) or 0)
    return ETFInfo(
        etf_code=code,
        etf_name=g(info, "etfName", "基金名称", default=code),
        report_unit=unit,
        cash_balance=float(g(info, "ecc", "现金差额", default=0.0) or 0.0),
        enable_creation=bool(int(g(info, "enableCreation", "是否允许申购", default=1) or 1)),
        enable_redemption=bool(int(g(info, "enableRedemption", "是否允许赎回", default=1) or 1)),
        components=components,
        nav_per_cu=float(g(info, "navPerCU", default=0.0) or 0.0),  # 每篮子净值，现金差额偏差估算用
    )


def _looks_like_timetag(k) -> bool:
    """判断 dict 的 key 是否像时间戳（8位数字），用于剥离 {timetag: info} 包裹。"""
    if isinstance(k, int):
        return True
    return isinstance(k, str) and k.isdigit() and len(k) >= 8


class QMTDataSource(DataSource):
    """银河大QMT 策略环境适配器。

    依赖大QMT 策略编辑器内置全局函数（无需 import 任何库）：
      - get_etf_info(code)   -> 申赎清单（实测结构见 parse_qmt_etf_info 注释）
      - get_snapshot(code)   -> 最新行情快照（实测 get_full_tick 不存在！）
      - get_etf_iopv(code)   -> 官方 IOPV（float）
      - ContextInfo.get_market_data_ex(['close'], [code], period='tick', count=1)  # 兜底

    注意：get_snapshot 的字段名在不同 QMT 版本有差异（lastPrice/bidPrice/askPrice
    或中文"最新价/买一价"），解析做了多字段兼容；若仍取不到，请把策略启动时
    打印的 get_snapshot 返回结构贴回来微调。
    """

    def __init__(self, context_info=None, strategy_globals=None):
        self._context = context_info
        # 大QMT 内置全局函数（如旧版 get_etf_info）注入在策略 exec 的命名空间里，
        # data_source.py 模块内部直接查 globals() 找不到，必须由策略把 globals() 传进来。
        self._strategy_globals = strategy_globals

    # ---- 申赎清单 ----
    def get_etf_info(self, code: str) -> Optional[ETFInfo]:
        raw = self._call_qmt("get_etf_info", code)   # 全局函数/ContextInfo 方法双路径
        return parse_qmt_etf_info(raw, code)

    def _call_qmt(self, fn_name, *args):
        """防御式调用大QMT 接口：新版本是 ContextInfo 方法，旧版本是策略命名空间里的全局函数。
        实测：升级 QMT Python 库后 get_etf_info 从全局函数变为 ContextInfo 方法（NameError 实证）。"""
        # 路径1：ContextInfo 方法（新版，与 get_full_tick 同风格）
        if self._context is not None:
            try:
                fn = getattr(self._context, fn_name, None)
                if fn is not None:
                    result = fn(*args)
                    if result:
                        return result
            except Exception:
                pass
        # 路径2：策略命名空间里的全局函数（旧版 QMT 注入）
        ns = self._strategy_globals if self._strategy_globals is not None else globals()
        try:
            fn = ns.get(fn_name) if isinstance(ns, dict) else getattr(ns, fn_name, None)
            if fn is not None:
                result = fn(*args)
                if result:
                    return result
        except Exception:
            pass
        return None

    # ---- 行情 ----
    def get_quotes(self, codes: List[str]) -> Dict[str, Quote]:
        """批量取行情（对齐用户 LOF 程序实测用法：ContextInfo.get_full_tick 批量调用）。"""
        ticks = self._get_ticks_batch(codes)
        out: Dict[str, Quote] = {}
        for code, tk in ticks.items():
            out[code] = Quote(
                last=_f(tk, "lastPrice", "last", "最新价", "最新"),
                bid1=_f(tk, "bidPrice", "bid1", "买一价"),
                ask1=_f(tk, "askPrice", "ask1", "卖一价"),
                prev_close=_f(tk, "lastClose", "preClose", "昨收"),
                halted=bool(tk.get("halted", False)),
                limit_up=bool(tk.get("upLimit", False)),
                limit_down=bool(tk.get("downLimit", False)),
            )
        return out

    def _get_ticks_batch(self, codes: List[str]) -> Dict[str, dict]:
        """批量行情。主路径：ContextInfo.get_full_tick(codes)（LOF 程序 V5 实测可用）；
        失败逐只回退 get_snapshot；再退 get_market_data_ex(tick)。"""
        if not codes:
            return {}
        # 主路径：批量全推 tick（用户 LOF 程序 Test_Yinhe_qmt_ServerV5 验证过）
        if self._context is not None:
            try:
                with self._api_lock():
                    ticks = self._context.get_full_tick(list(codes))
                if ticks:
                    return dict(ticks)
            except Exception:
                pass
        # 回退1：逐只 get_snapshot
        out = {}
        try:
            for code in codes:
                tk = get_snapshot(code)
                if tk:
                    out[code] = tk
            if out:
                return out
        except Exception:
            pass
        # 回退2：get_market_data_ex(tick) 只给 close
        try:
            if self._context is not None:
                md = self._context.get_market_data_ex(["close"], list(codes), period="tick", count=1)
                for code in codes:
                    if code in md and len(md[code]) > 0:
                        last = float(md[code].iloc[-1]["close"])
                        out[code] = {"lastPrice": last, "bidPrice": [last], "askPrice": [last]}
        except Exception:
            pass
        return out

    def _api_lock(self):
        """简单 API 锁，避免多 timer 竞争 C++ 接口（对齐用户 V5 的 api_lock）。"""
        if getattr(self, "_lock", None) is None:
            import threading
            self._lock = threading.Lock()
        return self._lock

    def _get_tick(self, code: str) -> Optional[dict]:
        """单只行情（兼容旧调用）。"""
        return self._get_ticks_batch([code]).get(code)

    def get_etf_price(self, code: str) -> float:
        tk = self._get_tick(code)
        if tk:
            return _f(tk, "lastPrice", "last", "最新价", "最新")
        return 0.0

    def get_official_iopv(self, code: str) -> Optional[float]:
        v = self._call_qmt("get_etf_iopv", code)   # 全局函数/ContextInfo 方法双路径
        try:
            return float(v) if v else None
        except Exception:
            return None


def _f(d: dict, *names, default: float = 0.0) -> float:
    """从行情 dict 里取数值字段，兼容标量/单元素列表。"""
    for n in names:
        if n in d and d[n] is not None:
            v = d[n]
            if isinstance(v, (list, tuple)):
                if len(v) > 0:
                    try:
                        return float(v[0])
                    except Exception:
                        return default
            else:
                try:
                    return float(v)
                except Exception:
                    return default
    return default
