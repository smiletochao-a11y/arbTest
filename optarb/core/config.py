# -*- coding: utf-8 -*-
"""Optarb 全局配置"""
import os

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ_PARENT = os.path.dirname(_CORE_DIR)
if os.path.basename(_PROJ_PARENT) == "src":
    ROOT = os.path.dirname(_PROJ_PARENT)      # 本地: Optarb/
else:
    ROOT = _PROJ_PARENT                       # ARM 扁平: /home/ubuntu/optarb
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "optarb.db")
LOG_DIR = os.path.join(ROOT, "log")

# 服务端口（避开 future:8103 / ETFarb:8102 / ArbDashboard:8000）
PORT = int(os.environ.get("OPTARB_PORT", "8104"))

# ---------------- 合约参数 ----------------
# 全局乘数（★ 仅用于股指期权的保证金公式 risk.py，保持 100 元/点）。
# 各品种的「合约乘数」已下放到 VARIETIES[v]["multiplier"]，
# 移仓价差/名义/IV 全部走每品种乘数，不再依赖这个全局值。
MULTIPLIER = 100

# 品种 -> 参数
#   index          标的（新浪代码；股指=指数 / ETF=ETF 价格）
#   name           显示名
#   exchange       CFFEX（股指）/ SSE（上交所 ETF）/ SZSE（深交所 ETF，Phase 2）
#   multiplier     合约乘数：股指 100 元/点 / ETF 10000 份·元/张
#   expiry_weekday 到期日星期（股指=4 周五 / ETF=2 周三）
#   expiry_weeknum 到期日是第几个该星期（股指=3 第三个 / ETF=4 第四个）
#   strike_unit    行权价单位（点 / 元），仅用于前端显示
#
# ★ 2026-09：新增 ETF 期权（东哥需求）。Phase 1 先上最熟的
#   上交所 300ETF / 500ETF 跑通「移仓价差 + 名义」；创业板/科创板留 Phase 2。
#   计算层（migrate/live）原本就品种无关，加 ETF 主要是：乘数 + 周三到期 + 现货符号。
VARIETIES = {
    "IO": {"index": "sh000300", "name": "沪深300股指期权", "exchange": "CFFEX",
           "multiplier": 100, "expiry_weekday": 4, "expiry_weeknum": 3, "strike_unit": "点"},
    "HO": {"index": "sh000016", "name": "上证50股指期权", "exchange": "CFFEX",
           "multiplier": 100, "expiry_weekday": 4, "expiry_weeknum": 3, "strike_unit": "点"},
    "MO": {"index": "sh000852", "name": "中证1000股指期权", "exchange": "CFFEX",
           "multiplier": 100, "expiry_weekday": 4, "expiry_weeknum": 3, "strike_unit": "点"},
    # ── ETF 期权（上交所）── 合约单位 10000 份/张；到期 = 第 4 个周三
    #    ★ 2026-09-29 东哥要：显示名改成**券商标准名**（银河APP 的叫法），内部码不变。
    #      命名规则 = 「标的ETF名（交易所）」，与银河一致，避免自造简称看不懂。
    "E300": {"index": "sh510300", "name": "沪深300ETF（沪）", "exchange": "SSE",
             "multiplier": 10000, "expiry_weekday": 2, "expiry_weeknum": 4, "strike_unit": "元"},
    "E500": {"index": "sh510500", "name": "中证500ETF（沪）", "exchange": "SSE",
             "multiplier": 10000, "expiry_weekday": 2, "expiry_weeknum": 4, "strike_unit": "元"},
    # ── ETF 期权（上交所，科创50ETF）── 588080 是上交所（不是深交所！）
    #    SSE 官方端点已返回该标的合约，扩 ETF_UNDERLYING_TO_VARIETY 即可接入。
    "E880": {"index": "sh588080", "name": "科创50ETF（沪）", "exchange": "SSE",
             "multiplier": 10000, "expiry_weekday": 2, "expiry_weeknum": 4, "strike_unit": "元"},
    # ── ETF 期权（深交所，创业板ETF）── 159915 是深交所；走 core/etf_szse.py
    #    szse.cn 的 option_drhy 只给「前结算价」(qjsjg)，无当日收盘/结算，也无现货；
    #    现货由 etf_szse 从新浪 sz159915 取。详见 docs/008 §四。
    "E915": {"index": "sz159915", "name": "创业板ETF（深）", "exchange": "SZSE",
             "multiplier": 10000, "expiry_weekday": 2, "expiry_weeknum": 4, "strike_unit": "元"},
}

# ETF 期权标的代码 -> 品种（上交所 commonQuery 端点用标的 6 位代码标识）
ETF_UNDERLYING_TO_VARIETY = {
    "510300": "E300",
    "510500": "E500",
    "588080": "E880",   # 科创50ETF（上交所，Phase 2a 接入）
}

# 对标期货（用于对冲/基差参考）
FUTURES_MAP = {"IO": "IF", "HO": "IH", "MO": "IM"}

# ---------------- 中金所行情源 ----------------
CFFEX_QUOTE_URL = "http://www.cffex.com.cn/quote_{variety}.txt"
CFFEX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Referer": "http://www.cffex.com.cn/",
}

# 行情缓存秒数（避免频繁打中金所）
# ★ 2026-10-03：这只是**盘中**的缓存秒数。非盘中（午休/盘前/收盘后/周末/假期）
#   实际 TTL 由 `core/market_calendar.quote_ttl()` 动态算成「到下一次可能变化」，
#   见 `quotes._cached()`。所以整个周末只会爬 1 次中金所。
QUOTE_CACHE_SEC = int(os.environ.get("OPTARB_QUOTE_CACHE", "30"))

# ---------------- 风险测算参数 ----------------
# 情景分析的涨跌幅档位（%），正数=涨
SCENARIOS = [-30, -20, -15, -10, -8, -5, -3, -2, -1, 0, 1, 2, 3, 5, 8, 10, 15, 20, 30]

# "废纸"保护的档位：距现价的虚值幅度（%）
LADDER = [3, 5, 8]

# ---------------- 未来休市日（★ 每年需补一次）----------------
# 为什么需要：算「当月还剩几个交易日」时，**未来**的法定假日必须排掉。
# 历史部分可以从指数日线反推（见 live.holiday_set），但未来没有行情可反推。
#
# 来源：沪深北交易所每年 12 月发布的《次年部分节假日休市安排》公告。
#   · 只需维护「已公告、但还没有行情」的那一段（窗口最长 20 个交易日，通常一个假期就够）
#   · 每年 12 月新公告出来后，把次年的安排补进来
#   · 格式：YYYY-MM-DD 的休市日（只列**工作日**，周末本来就不算）
#
# ⚠️ 2026-09-27 实测踩坑：国庆 10-01~10-07 没排掉，导致「当月还剩 15 个交易日」
#    （真实是 10 个）→ 历史窗口对齐到 T-15 而不是 T-10 → 分位从 50.0% 虚高到 77.3%。
HOLIDAYS_FUTURE = {
    "2026": [
        "2026-10-01", "2026-10-02", "2026-10-05",
        "2026-10-06", "2026-10-07",
    ],
}
