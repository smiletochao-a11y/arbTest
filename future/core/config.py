# -*- coding: utf-8 -*-
"""
future 股指期货贴水看板 - 全局配置
"""
import os

# ============ 路径 ============
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ_PARENT = os.path.dirname(_CORE_DIR)
if os.path.basename(_PROJ_PARENT) == "src":
    SRC_ROOT = _PROJ_PARENT                       # 本地: future/src
    PROJ_ROOT = os.path.dirname(_PROJ_PARENT)      # 本地: future/
else:
    SRC_ROOT = _PROJ_PARENT                        # ARM 扁平: future/
    PROJ_ROOT = _PROJ_PARENT
ROOT = PROJ_ROOT
DATA_DIR = os.path.join(PROJ_ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
DB_PATH = os.path.join(DATA_DIR, "future.db")
WEB_DIR = os.path.join(SRC_ROOT, "frontend")
LOG_DIR = os.path.join(PROJ_ROOT, "log")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ============ 数据后端 ============
# "db"  = 读 SQLite（data/future.db）—— 默认，见 docs/006
# "csv" = 读 data/cache/*.csv  —— 回退开关，排障时可切回
# 也可用环境变量 DATA_BACKEND 覆盖（方便线上临时切换而不用改代码）
DATA_BACKEND = os.environ.get("DATA_BACKEND", "db").strip().lower()
if DATA_BACKEND not in ("db", "csv"):
    DATA_BACKEND = "db"

# ============ 服务 ============
DEFAULT_PORT = 8103   # 避开 ETFarb 的 8102

# ============ 品种配置 ============
# 本表是品种定义的**唯一真值来源**（backend/fetch_data.py 直接 import 本表，
# 不再各存一份，避免两边漂移）。
#
#   name         中文名
#   exchange     "cffex"=中金所（股指） / "dce"=大商所（铁矿石）
#                —— 决定「最后交易日」规则，见 data_source.symbol_last_trade_date()
#   spot_code    新浪指数代码；**铁矿石为 None**（新浪没有铁矿石现货指数，
#                现货另走生意社 100ppi.com，见 docs/009 §1.1/§1.2）
#   start        首个有数据的合约月份（逐合约日线的起点，受新浪"只留约 8 年"限制）
#   mult         合约乘数（IC/IM 200 元/点，IF/IH 300 元/点，铁矿石 100 吨/手）
#   unit         价格单位（"点" / "元/吨"）—— 前端展示用，别混
#   months_ahead 同时挂牌的最远合约约在几个月后（用于增量抓取的上界筛选）
#   active_mode  「取哪几个在交易合约」的规则，见 data_source.get_active_symbols()：
#                 "near" = 按合约月份由近到远取（当月/次月/当季/下季）—— 股指用
#                 "hold" = 按**持仓量**由大到小取（主力/次主力/…）—— 铁矿石用
#   active_count 取几个（默认 4）
VARIETIES = {
    "IC": {"name": "中证500",  "exchange": "cffex", "spot_code": "sh000905",
           "start": "2015-04-16", "mult": 200, "unit": "点",    "months_ahead": 8,
           "active_mode": "near", "active_count": 4},
    "IM": {"name": "中证1000", "exchange": "cffex", "spot_code": "sh000852",
           "start": "2022-07-22", "mult": 200, "unit": "点",    "months_ahead": 8,
           "active_mode": "near", "active_count": 4},
    "IF": {"name": "沪深300",  "exchange": "cffex", "spot_code": "sh000300",
           "start": "2010-04-16", "mult": 300, "unit": "点",    "months_ahead": 8,
           "active_mode": "near", "active_count": 4},
    "IH": {"name": "上证50",   "exchange": "cffex", "spot_code": "sh000016",
           "start": "2015-04-16", "mult": 300, "unit": "点",    "months_ahead": 8,
           "active_mode": "near", "active_count": 4},
    # ⚠️ 铁矿石：代码**小写** `i`（交易所就是小写；新浪日线大小写都认，
    #    但实时接口 hq.sinajs.cn 只认大写 nf_I2609 —— 见 docs/009 差异 3）
    # ⚠️ active_mode="hold"：铁矿石同时挂 **12 个月份**（不是股指的 4 个），
    #    但主力只在 01/05/09 之间轮换 → 「近月」常常是没人交易的空壳，
    #    必须按持仓量选，不能按月份选。见 docs/009 差异 1 / §6 第二步。
    "i":  {"name": "铁矿石",   "exchange": "dce",   "spot_code": None,
           "start": "2018-01-01", "mult": 100, "unit": "元/吨", "months_ahead": 13,
           "active_mode": "hold", "active_count": 4},
}

# 默认只显示 IC/IM（用户关注的两个）
DEFAULT_VARIETIES = ["IC", "IM"]

# ============ 已完成端到端接入的品种（前端会展示） ============
# ⚠️ 与 VARIETIES 的区别：VARIETIES 是「**数据层认得**的全部品种」，
#    ENABLED_VARIETIES 是「**计算层+前端也已经接好**、可以安全展示的品种」。
#
# 铁矿石（i）目前**只完成了第一步：日线入库**（见 docs/009 §6）。
# 它的现货/基差/主力选取还没接，点进去会是空表，所以**先不放进这里**。
# 等 §6 第二、三步做完，把 "i" 加进来即可 —— 这是唯一的开关。
ENABLED_VARIETIES = ["IC", "IM", "IF", "IH"]

# ============ 计算口径（关键！统一定义，避免混乱） ============
# 基差 = 现货收盘价 - 期货收盘价
#   > 0 → 贴水（期货比现货便宜）
#   < 0 → 升水
# 折合年化 = (基差 / 现货价) * (365 / 剩余天数) * 100%
#
# 价差（移仓用）= 近月价格 - 远月价格（与 Wind / 集思录一致，见 engine.pair_spread_series）
#   > 0 → 远月更便宜（贴水结构），移仓可获贴水收益
#   < 0 → 远月更贵（升水结构），移仓需付出成本
# 价差年化 = (价差 / 近月价格) * (12 / 月份差) * 100%
#   ⚠️ 与「基差年化」的 365/剩余天数 不是同一口径，前端必须分开标注。
#   ⚠️ 铁矿石用「主力 − 次主力」（跨 4 月档），与股指的「当月 − 次月」档位不同，
#      见 docs/009 差异 1。
BASIS_DEF = "现货收盘价 - 期货收盘价"
