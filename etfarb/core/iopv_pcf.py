#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/iopv_pcf.py  —  PCF 数据模型 + 抓取/粘贴 + **唯一落库出口**
=============================================================
独立模块，专门服务 ETFarb IOPV 引擎。

🔴 职责边界（2026-09-16 重新表述，取代旧的"与 core/pcf_fetcher.py 互不相干"）：

  落库层 = 本模块 `PCFFetcher._persist()`，**两个市场共用一套**写入口
      （`pcf_snapshot` + `pcf_component`），配套 `check_today_complete()` /
      `count_components()` / `repair()` 自动对两个市场生效。
      红线本意是"**不出现两套落库 / 两套口径**"（两套 INSERT 各管一个市场，
      改一次表结构要改两处 —— "有 snapshot 无成分"半残行的温床），**不是禁止函数复用**。

  取数 + 解析层：上交所 XML 在本模块（`fetch_sse` / `parse_xml`）；
      深交所宽表 txt 复用 `core/pcf_fetcher` 的**纯解析函数**（不写库、不依赖本模块）。
      依赖方向单向：`iopv_pcf` → `pcf_fetcher`。

满足方案 P1：
  · 初始化 etfarb.db 新增 6 张表（pcf_snapshot / pcf_component / fund_iopv_config /
    iopv_point / iopv_verify / iopv_close）
  · 按 `fund_iopv_config.pcf_source` 分派抓取：
      `sse_xml`  → query.sse.com.cn（上交所，只给最新一份；历史走鹏华归档，见 docs/20）
      `szse_txt` → reportdocs.static.szse.cn/files/text/etf/ETF{code}{YYYYMMDD}.txt
                   （深交所 GBK 宽表，**历史可回溯到上市首日**）
  · 支持用户粘贴 PCF（XML 或 官网复制的表格文本）落库
  · seed 513590 的 9-10 / 9-11 历史 PCF（来自已验证脚本）

⚠️ 键位铁律（东哥 2026-09-14 钉死）：
  pcf_snapshot.trade_date     = 公告日 TradingDay
  pcf_snapshot.pre_trade_date = 信息内容日 PreTradingDay = **篮子定价基准日**
  pcf_component.trade_date    = 所属公告的 TradingDay（成分价格基准 = 该公告的 pre_trade_date）
  ⇒ 想拿「基准日 D 的篮子」→ 找 pre_trade_date=D 的那份公告，取它的成分。
  （深交所 txt 自带两个日期段，直接映射，不靠交易日历推算。）

用法：
  python iopv_pcf.py                  # 完整流水线：建表→修复(幂等)→抓最新→展示
  python iopv_pcf.py --init          # 仅建表 + 配置
  python iopv_pcf.py --repair        # 一键修复：按 raw_text 重建成分 + 补齐 seed 篮子（幂等）
  python iopv_pcf.py --code 513590 --fetch
  python iopv_pcf.py --code 159366 --fetch --date 20260916
  python iopv_pcf.py --code 513590 --paste path.txt
  python iopv_pcf.py --seed-historical
  python iopv_pcf.py --show --code 513590
"""
import os
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
import datetime
import argparse
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import db as _db   # noqa: E402  core/db.py —— 单库方案 arb_master.db
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

try:
    import pcf_fetcher as pf        # 深交所纯解析层（本模块单向依赖它）
except Exception:                    # 独立运行/路径未配好时不致命
    pf = None

SSE_URL = "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={code}"

# ----------------------------------------------------------------------
# 官方 PCF 直链（2026-09-28 加，供前端主看板「PCF」列点击）
# ----------------------------------------------------------------------
# 🔴 两市发布口子不同，**按市场分派，不可互套**（2026-09-28 实测）：
#   · 深市 ETF（158/159xxx）→ 静态 CDN 宽表 txt，**日期必填**
#       https://reportdocs.static.szse.cn/files/text/etf/ETF{code}{YYYYMMDD}.txt
#       响应头 `Content-Type: text/plain; charset=GBK` ⇒ 浏览器可直接读中文。
#       ⚠️ 非交易日 / 尚未发布 = HTTP 404 ⇒ 日期必须取「最近可得交易日」，不能写死今天。
#       ⚠️ 深市 LOF（16xxxx）走同一路径**一律 404**（实测 160119/161725）⇒ 不给链接。
#   · 沪市 ETF（51/52/56/58xxxx）→ 上交所下载口，**无日期参数 = 永远返回最新一份**
#       https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={code}
#       ⚠️🔴 该口对**非沪市 ETF 代码**（159509 / 501050 / 160119 …）**也返 HTTP 200**，
#          body 却是 105 字节错误模板：`({"jsonCallBack":"null","success":"false",…})`
#          ⇒ 判据**只能看代码前缀**，绝不能用返回码判断"是否支持"。
#       ⚠️🔴 该口**不能直接当链接**（东哥 2026-09-28 实测）：它的响应头是
#             `Content-Type: application/octet-stream`
#             `Content-Disposition: attachment; filename="ssepcf_{code}_{date}.xml"`
#             `X-Download-Options: noopen`
#          ⇒ 浏览器收到就**另存文件**，不会在新窗口里渲染（对比深市 CDN 是
#             `text/plain; charset=GBK` 且无 Content-Disposition ⇒ 直接内联可读）。
#          故沪市一律改走**本机代理** `/api/pcf/view`（backend/app.py:_send_pcf_view）：
#          只转发原文、剥掉下载头，新窗口即可直接阅读。
SZSE_PCF_CDN = "https://reportdocs.static.szse.cn/files/text/etf/ETF{code}{date}.txt"
SSE_VIEW_PATH = "/api/pcf/view?code={code}"        # 沪市 PCF：本机代理（相对路径，自动跟当前 host）
_SZSE_ETF_PREFIX = ("158", "159")
_SSE_ETF_PREFIX = ("51", "52", "56", "58")
_SZSE_DATE_CACHE = {"date": "", "ts": 0.0}
_SZSE_DATE_TTL = 1800                      # 30 分钟（日内发布后自动前进一天）
_SZSE_PROBE_REFS = ("159919", "159915")    # 沪深300ETF/创业板ETF：深市巨型 ETF，逐交易日必发


def is_szse_etf(code):
    return str(code or "").strip().upper().split(".")[0][:3] in _SZSE_ETF_PREFIX


def is_sse_etf(code):
    return str(code or "").strip().upper().split(".")[0][:2] in _SSE_ETF_PREFIX


def _probe_szse_date(ref_code, date8, timeout=6):
    """ref_code 在该日是否有深交所 PCF（只看状态码，不读 body）。"""
    url = SZSE_PCF_CDN.format(code=ref_code, date=date8)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200) or 200) == 200
    except Exception:
        return False


def latest_szse_pcf_date(force=False):
    """最近一份「已发布」的深交所 PCF 日期（YYYYMMDD）。

    交易日当天约 08:30 起 CDN 即有当日文件；非交易日 / 尚未发布则向前回溯最多 10 天
    （覆盖周末 + 长假）。结果模块内缓存 30 分钟。
    探测全失败（网络不可达）时退回今天，并让缓存提前过期以便重试 —— 不阻断看板。"""
    now = time.time()
    if (not force and _SZSE_DATE_CACHE["date"]
            and (now - _SZSE_DATE_CACHE["ts"]) < _SZSE_DATE_TTL):
        return _SZSE_DATE_CACHE["date"]
    d = datetime.date.today()
    found = ""
    for _ in range(10):
        ds = d.strftime("%Y%m%d")
        if any(_probe_szse_date(ref, ds) for ref in _SZSE_PROBE_REFS):
            found = ds
            break
        d -= datetime.timedelta(days=1)
    if found:
        _SZSE_DATE_CACHE.update(date=found, ts=now)
        return found
    ds = datetime.date.today().strftime("%Y%m%d")
    _SZSE_DATE_CACHE.update(date=ds, ts=now - _SZSE_DATE_TTL + 300)   # 5 分钟后重试
    return ds


def official_pcf_url(code, date8=None):
    """前端「PCF」列要用的可点击地址（深市 = CDN 直链 / 沪市 = 本机内联代理）；无源的返回 None。

    ⚠️ 只按**代码前缀**分派（见上方红线），不查 `fund_iopv_config`
       —— 该表只登记了 3~4 只监控标的，全市场 200+ 只靠前缀才覆盖得全。
    ⚠️ 沪市返回的是**相对路径**（`/api/pcf/view?code=…`）而非上交所上游 URL —— 原因见上方红字
       （上游带 `Content-Disposition: attachment`，直链只会触发下载）。"""
    c = str(code or "").strip().upper().split(".")[0]
    if not c:
        return None
    if c[:3] in _SZSE_ETF_PREFIX:
        return SZSE_PCF_CDN.format(code=c, date=(str(date8) if date8 else latest_szse_pcf_date()))
    if c[:2] in _SSE_ETF_PREFIX:
        return SSE_VIEW_PATH.format(code=c)
    return None


class PCFUnavailable(Exception):
    """某交易日的 PCF 取不到（非交易日 / 尚未发布 / 源站限流）。绝不兜底造数。"""
    pass


# 先跑通一条；其他 ETF 后续按此模板注册
DEFAULT_CONFIGS = {
    "513590": dict(
        fund_name="鹏华中证港股通消费主题ETF",
        kind="跨境QDII",
        pcf_source="sse_xml",
        fx_currency="HKD/CNY",
        fx_rate=0.864,
        creation_redemption_unit=300000,
        quote_source="tencent_hk",
        premium_threshold_bps=100,
        discount_threshold_bps=100,
        enabled=1,
    ),
    # 159366 永赢中证港股通医疗主题ETF（深交所跨境ETF，2026-09-16 接入）
    # 🔴 阈值 50bp（不是 513590 的 100bp）：它的折溢价 σ 只有 ~48bp，且 8~9 月
    #    汇率楔子 −0.5%~−1.1% 会把真实溢价在**表观**口径上藏掉 ——
    #    实测 136 日表观溢价从未突破 100bp ⇒ 沿用 100bp 这条线等于没装。
    #    判据同时改为「实际折溢价」（见 backend/app.py:_iopv_realtime）。
    "159366": dict(
        fund_name="永赢中证港股通医疗主题ETF",
        kind="港股通",
        pcf_source="szse_txt",
        fx_currency="HKD/CNY",
        fx_rate=0.863,
        creation_redemption_unit=1000000,
        quote_source="tencent_hk",
        premium_threshold_bps=50,
        discount_threshold_bps=50,
        enabled=1,
    ),
    # 513000 易方达奥明日经225ETF（沪市跨境QDII，2026-09-16 接入）
    # 🔴 结构 = **单券 FoF**：整篮只有 1 只成分 —— 东京上市的 1330
    #    「上場インデックスファンド225」（Amova，实物复制日经225，净资产 8.3 万亿日元）378 股。
    #    全现金替代（MaxCashRatio=1）、**申购现金替代溢价比例 20%**
    #    —— 是 513590(15%) / 513500(4%) 的数倍，押金属性更强。
    # 🔴 净值基准日 D = **T-1**（PRETRADINGDAY 13/13 期恒定）—— **跨境 ≠ 一律 T-2**：
    #    港股通 / 日本 = T-1，美股 = T-2（成因见 docs/21 §二·D）。
    # 🔴 取价：东京 1330 **拿不到实时行情**（腾讯 `jp1330` 空、新浪无日股个股）
    #    ⇒ `quote_source="index_proxy"`：篮子按 **日经225 指数比 × JPY/CNY 比** 缩放，
    #    锚点是 `NAVperCU − PreCashComponent`（= 官方篮子货值，无需成分明细）。
    # ⚠️ 阈值未观测（σ 未知），暂与 513590 同档 100bp；9-16 实测溢价 +4.58%（指数代理口径）。
    # ✅ 2026-09-16 已实测通过：数据层 23 期落库（8-17~9-16）+ 指数代理通道跑通 ⇒ enabled=1。
    "513000": dict(
        fund_name="易方达奥明日经225ETF",
        kind="跨境QDII",
        pcf_source="sse_xml",
        fx_currency="JPY/CNY",
        fx_rate=0.043419,
        creation_redemption_unit=500000,
        quote_source="index_proxy",
        index_key="N225",
        premium_threshold_bps=100,
        discount_threshold_bps=100,
        enabled=1,
    ),
}


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------
def conn(db=DB_PATH):
    return sqlite3.connect(db)


def init_db(db=DB_PATH):
    c = conn(db)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pcf_snapshot(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fund_code TEXT NOT NULL,
      trade_date TEXT NOT NULL,
      pre_trade_date TEXT,
      raw_text TEXT,
      cash_diff REAL,
      est_cash_part REAL,
      unit_nav REAL,
      nav REAL,
      units INTEGER,
      max_cash_ratio REAL,
      creation_limit INTEGER,
      redemption_limit INTEGER,
      -- 🔴 额度三兄弟（2026-09-16 新增，实证见 docs/21 §1.1）：
      --   沪市「账户级额度」是**可选字段**，513500 有 CreationLimitPerAcct、513590 无；
      --   易方达系（513000）下发的是**净额口径** NetCreationLimit(+PerAcct)，
      --   与「累计」口径 CreationLimit **不可直接比大小**。
      creation_limit_per_acct INTEGER,
      net_creation_limit INTEGER,
      net_creation_limit_per_acct INTEGER,
      record_number INTEGER,
      source TEXT,
      fetched_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pcf_snap ON pcf_snapshot(fund_code, trade_date);

    CREATE TABLE IF NOT EXISTS pcf_component(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_id INTEGER NOT NULL,
      fund_code TEXT NOT NULL,
      trade_date TEXT NOT NULL,
      stock_code TEXT NOT NULL,
      name TEXT,
      shares INTEGER,
      cash_sub_flag INTEGER,
      premium_ratio REAL,
      discount_ratio REAL,
      subst_amount REAL,
      -- 🔴 「必须现金替代」成分的**货值**（CNY，一个篮子口径），2026-09-17 立。
      --    深市 PCF 里这类行的 `股票数量`=0，货值写在 `申购替代金额` 列 ⇒ 若只按
      --    shares>0 取成分，**整只成分会被漏出篮子**（见 docs/40 §七·15）。
      --    ⚠️ 与 subst_amount 分工：subst_amount 记「官方篮子货值」（深市记在虚拟行
      --    159900，校准用），cash_value 记「单只成分的现金替代货值」（实时估值用）。
      cash_value REAL,
      market TEXT,
      FOREIGN KEY(snapshot_id) REFERENCES pcf_snapshot(id)
    );
    CREATE INDEX IF NOT EXISTS idx_pcf_comp ON pcf_component(fund_code, trade_date);

    CREATE TABLE IF NOT EXISTS fund_iopv_config(
      fund_code TEXT PRIMARY KEY,
      fund_name TEXT,
      kind TEXT,
      pcf_source TEXT,
      fx_currency TEXT,
      fx_rate REAL,
      creation_redemption_unit INTEGER,
      quote_source TEXT,
      -- 「单券 FoF」走指数代理时的跟踪指数键（如 N225），其余标的为 NULL（2026-09-16）
      index_key TEXT,
      premium_threshold_bps INTEGER,
      discount_threshold_bps INTEGER,
      enabled INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS iopv_point(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fund_code TEXT NOT NULL,
      ts TEXT,
      trade_date TEXT,
      iopv REAL,
      market_price REAL,
      premium_bps REAL,
      source TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_iopv_point ON iopv_point(fund_code, ts);

    CREATE TABLE IF NOT EXISTS iopv_verify(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fund_code TEXT NOT NULL,
      trade_date TEXT NOT NULL,
      computed_basket REAL,
      official_basket REAL,
      error_bps REAL,
      fx_used REAL,
      status TEXT,
      UNIQUE(fund_code, trade_date)
    );

    -- 「收盘 IOPV」（2026-09-15 新增，兑现东哥口径）：
    --   键 = 公告日 T（= 该 IOPV 所属交易日）
    --   iopv(T收盘) = [ Σ(T公告成分股数 × T日港股不复权收盘 × T日中间价) + T公告预估现金 ] ÷ 申赎单位
    --   篮子与预估现金同源（同一份 T 公告）；与主看板实时 IOPV 同口径（收盘时刻二者相等）。
    --   ⚠️ 与 iopv_verify 的区别：verify 键=篮子基准日 D，是「复现官方 Σ替代金额」的算法校准；
    --      本表键=公告日 T，是「T 日收盘时点的 IOPV」的展示值。两者不是一回事。
    CREATE TABLE IF NOT EXISTS iopv_close(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fund_code TEXT NOT NULL,
      trade_date TEXT NOT NULL,
      basket REAL,
      est_cash REAL,
      units INTEGER,
      iopv REAL,
      mid_fx REAL,
      n_comps INTEGER,
      status TEXT,
      note TEXT,
      computed_at TEXT,
      UNIQUE(fund_code, trade_date)
    );
    """)
    _ensure_column(c, "fund_iopv_config", "kind", "TEXT")
    _ensure_column(c, "fund_iopv_config", "index_key", "TEXT")
    # 老库补「必须现金替代」成分货值列（2026-09-17 修 #15 时新增）
    _ensure_column(c, "pcf_component", "cash_value", "REAL")
    # 老库补额度列（2026-09-16 接入 513000 时新增）
    _ensure_column(c, "pcf_snapshot", "creation_limit_per_acct", "INTEGER")
    _ensure_column(c, "pcf_snapshot", "net_creation_limit", "INTEGER")
    _ensure_column(c, "pcf_snapshot", "net_creation_limit_per_acct", "INTEGER")
    # 老库补默认 kind（只补空值，绝不覆盖用户已改）
    for code, cfg in DEFAULT_CONFIGS.items():
        c.execute("UPDATE fund_iopv_config SET kind=? WHERE fund_code=? AND (kind IS NULL OR kind='')",
                  (cfg.get("kind"), code))
    # seed config（INSERT OR IGNORE：不覆盖用户已改）
    for code, cfg in DEFAULT_CONFIGS.items():
        row = dict(cfg)
        row["fund_code"] = code
        row.setdefault("index_key", None)   # 老标的无此键，补 None
        c.execute(
            "INSERT OR IGNORE INTO fund_iopv_config "
            "(fund_code,fund_name,kind,pcf_source,fx_currency,fx_rate,creation_redemption_unit,"
            "quote_source,index_key,premium_threshold_bps,discount_threshold_bps,enabled) "
            "VALUES (:fund_code,:fund_name,:kind,:pcf_source,:fx_currency,:fx_rate,"
            ":creation_redemption_unit,:quote_source,:index_key,:premium_threshold_bps,"
            ":discount_threshold_bps,:enabled)",
            row,
        )
    c.commit()
    c.close()
    return db


def _ensure_column(c, table, col, decl):
    """幂等补列（老库升级用；CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。"""
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % table)]
        if col not in cols:
            c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    except Exception:
        pass


# ----------------------------------------------------------------------
# PCF 抓取 / 解析 / 落库
# ----------------------------------------------------------------------
class PCFFetcher:
    def __init__(self, db=DB_PATH):
        self.db = db

    # ---- 抓取 ----
    def fetch_sse(self, fund_code):
        url = SSE_URL.format(code=fund_code)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.sse.com.cn/",
        })
        raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        return raw

    # ---- 抓取：深交所宽表 txt（2026-09-16 接入） ----
    def fetch_szse(self, fund_code, date8):
        """抓某个交易日的深交所 PCF 文本。date8 = 'YYYYMMDD'，返回 (text, source)。

        源：reportdocs.static.szse.cn/files/text/etf/ETF{code}{YYYYMMDD}.txt（GBK 宽表）。
        复用 pcf_fetcher.fetch_pcf（它已按 CDN txt → 旧式 xml → ShowReport 顺序尝试）。
        非交易日 / 尚未发布 / 源站限流 → PCFUnavailable（绝不兜底造数）。"""
        if pf is None:
            raise PCFUnavailable("pcf_fetcher 未加载，无法抓深交所 PCF")
        text, url = pf.fetch_pcf(fund_code, str(date8))
        if not text:
            raise PCFUnavailable(
                "[%s] %s 无深交所 PCF（非交易日 / 尚未发布 / 源站限流）" % (fund_code, date8))
        return text, (url or "szse_txt")

    # ---- 抓取：易方达系沪市历史 PCF（JSON，2026-09-16 接入） ----
    EFUNDS_URL = ("https://api.efunds.com.cn/xcowch/front/etffund/baseinfo"
                  "?fundCode={code}&tDate={date}&listType=0")

    def fetch_efunds(self, fund_code, date_iso):
        """易方达系沪市历史 PCF（第三个沪市历史口子，直返 JSON）。

        ⚠️🔴 坑：**请求未发布的日期会「回退返回最新一份」** ⇒ 调用方必须用返回的
           `data.tDate` 校验，不等于请求日即视为该日无公告（parse_efunds 会抛）。"""
        url = self.EFUNDS_URL.format(code=fund_code, date=date_iso)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.efunds.com.cn/",
        })
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        if not raw or not raw.strip():
            raise PCFUnavailable("[%s] %s 易方达接口空响应" % (fund_code, date_iso))
        return raw

    def parse_efunds(self, text, want_date_iso=None):
        """易方达 JSON → (header, comps)。

        🔴 该接口**不含成分明细**（map 只有 RECORDNUM，没有 ComponentList）⇒ comps 恒为 []。
           对 513000 不构成缺口：**单券 + 全现金替代**时
           `篮子货值(D) = NAVPERCU − CASHCOMPONENT`，与官方 `Σ SubstitutionCashAmount`
           等价（9-16 实测 1,084,237.66，分毫一致）。
        🔴 额度是**净额口径**（NETCREATIONLIMIT / NETCREATIONLIMITPERUSER），
           与沪市「累计」口径 CreationLimit **不可直接比大小**。
        🔴 键位同沪市：`NAVPERCU` = D 日篮子净资产、`CASHCOMPONENT` 属 D、
           `ESTIMATECASHCOMPONENT` 属 T。"""
        import json as _json
        j = _json.loads(text)
        if not j.get("status"):
            raise PCFUnavailable("易方达接口 status!=1: %s" % str(j.get("message"))[:120])
        d = j.get("data") or {}
        got = str(d.get("tDate") or "").strip()
        code = (d.get("etfInfo") or {}).get("C_FUNDID")
        if want_date_iso and got and got != want_date_iso:
            raise PCFUnavailable(
                "[%s] 请求 %s，接口回退返回 %s（该日未发布）—— 不落库" % (code, want_date_iso, got))
        m = d.get("map") or {}

        def num(k):
            try:
                return float(m.get(k))
            except (TypeError, ValueError):
                return None

        def iso(v):
            s = str(v if v is not None else "").strip()
            if s.endswith(".0"):
                s = s[:-2]
            if len(s) == 10 and s[4] == "-":
                s = s.replace("-", "")
            return s if (len(s) == 8 and s.isdigit()) else ""

        def i(k):
            v = num(k)
            return int(v) if v is not None else None

        header = {
            "trade_date": iso(m.get("TRADINGDAY")) or self._sec_to_iso(got),
            "pre_trade_date": iso(m.get("PRETRADINGDAY")),
            "units": i("CREATIONREDEMPTIONUNIT"),
            "unit_nav": num("NAVPERCU"),
            "nav": num("NAV"),
            "cash_diff": num("CASHCOMPONENT"),
            "est_cash_part": num("ESTIMATECASHCOMPONENT"),
            "max_cash_ratio": num("MAXCASHRATIO"),
            # 额度口径按原文归位，**不混列**：
            #   9-07 之前易方达下发 CREATIONLIMIT（**累计**口径）；
            #   9-07 起换成 NETCREATIONLIMIT / NETCREATIONLIMITPERUSER（**净**口径）。
            "creation_limit": i("CREATIONLIMIT"),
            "redemption_limit": i("REDEMPTIONLIMIT"),
            "creation_limit_per_acct": i("CREATIONLIMITPERUSER"),
            "net_creation_limit": i("NETCREATIONLIMIT"),
            "net_creation_limit_per_acct": i("NETCREATIONLIMITPERUSER"),
            "record_number": i("RECORDNUM"),
        }
        if not header["trade_date"]:
            raise PCFUnavailable("[%s] 易方达返回缺 TradingDay，无法落库" % code)
        return header, []

    def fetch_efunds_and_store(self, fund_code, date8):
        """按日期回溯易方达系 PCF 并落库（历史回填入口）。返回 (snapshot_id, n_comps)。"""
        d8 = str(date8)
        iso = "%s-%s-%s" % (d8[:4], d8[4:6], d8[6:8])
        raw = self.fetch_efunds(fund_code, iso)
        header, comps = self.parse_efunds(raw, want_date_iso=iso)
        return self._persist(fund_code, header, comps, raw, "efunds_json")

    # ---- 解析：深交所宽表 → 本模块 _persist 的 (header, comps) 键位 ----
    @staticmethod
    def _sec_to_iso(d):
        """'2026-09-15' / '20260915' → '20260915'（落库统一无横线 8 位）。"""
        s = str(d or "").strip()
        if len(s) == 10 and s[4] == "-":
            s = s.replace("-", "")
        return s if (len(s) == 8 and s.isdigit()) else ""

    def parse_szse(self, text, fund_code):
        """深交所宽表 → (header, comps)。解析复用 pcf_fetcher 纯函数，本方法只做键位映射。

        🔴 映射口径（2026-09-16 定，详见 docs/21）：
          1) trade_date / pre_trade_date 直接取文本里**自带的两个日期段**
             （「YYYY-MM-DD日 信息内容」：前者 = T-1 段 = 篮子基准日，
               后者 = T 段 = 公告日）—— 不靠交易日历推算，零歧义。
          2) 深市组合明细**不给每股替代金额**（『申购替代金额』对普通成分整列 0.0000），
             官方篮子货值只体现在虚拟行 `159900 申赎现金`：
                 申购替代金额(159900) = (NAVperCU − 预估现金) × (1 + 保证金率)
             实测 159366：1,640,738.57 = (1,426,922.15 − 192.97) × 1.15（误差 0.01 元）。
             ⇒ 落库时把 159900 行的 subst_amount 记成**官方篮子货值**
                (= NAVperCU − 预估现金差额)，与上交所「Σ每股替代金额 = 官方篮子货值」
                口径完全一致（恒等式：NAVperCU = Σ替代金额 + 预估现金）。
             🔴 **但「必须现金替代」成分是例外**（2026-09-17 修 #15 查明）：这类行
                `股票数量`=0，『申购替代金额』/『赎回替代金额』两列**装的是该成分的货值**
                （159960 网易 = 25,444.36），旧的「整列 0.0000」假设是错的 ⇒
                落 `cash_value` 列（见 docs/40 §七·15）。
          3) stock_code 用**裸 5 位港股码**（不带 .SZ 后缀）—— 港股行情口是 hk00241；
             market：香港市场→HK，深圳市场→SZ，上海市场→SH。
        """
        if pf is None:
            raise PCFUnavailable("pcf_fetcher 未加载，无法解析深交所 PCF")
        info = pf.parse_pcf(text, fund_code)
        if "sec_date_trade" not in info:
            raise PCFUnavailable("[%s] 深交所返回内容非宽表 txt（无日期段），放弃落库" % fund_code)
        trade_date = self._sec_to_iso(info.get("sec_date_trade"))
        pre_td = self._sec_to_iso(info.get("sec_date_pre"))
        if not trade_date:
            raise PCFUnavailable("[%s] 深交所 txt 未解析出公告日，放弃落库" % fund_code)
        est = info.get("cash_balance")
        unit_nav = info.get("nav_per_cu")
        official_basket = (float(unit_nav) - float(est)
                           if (unit_nav is not None and est is not None) else None)
        header = {
            "trade_date": trade_date,
            "pre_trade_date": pre_td or None,
            "units": int(info.get("report_unit") or 0) or None,
            "unit_nav": unit_nav,
            "nav": info.get("nav_per_unit"),
            "cash_diff": info.get("cash_balance_t1"),
            "est_cash_part": est,
            "max_cash_ratio": info.get("cash_replace_cap"),
            "creation_limit": info.get("creation_limit"),
            "redemption_limit": info.get("redemption_limit"),
            "record_number": info.get("total_component_count"),
        }
        comps = []
        for c in info.get("components", []):
            raw = c.get("raw_code") or ""
            vol = int(c.get("volume") or 0)
            fixed_amt = c.get("fixed_amount")
            if raw == "159900":
                mkt, subst = "SZ", official_basket          # 见上文 2)
                cash_value = None
            else:
                # 🔴 深市宽表「挂牌市场」→ 市场归属（旧码 `{"深圳市场","上海市场"}.get(..., "HK")`
                #   把「其他市场」（美股/日股/韩股）兜底成了 HK ⇒ 159941 的 101 只美股被误标港股）。
                #   未登记值不猜（返回 ''，天然不等于 'HK'，分流时保守归入非逐券）。
                mkt = {"深圳市场": "SZ", "上海市场": "SH",
                       "香港市场": "HK", "其他市场": "OS"}.get((c.get("market") or "").strip(), "")
                subst = None                                # 深交所不给每股替代金额，不猜
                # 🔴 「必须现金替代」成分：`股票数量`=0，货值写在『申购替代金额』列
                #   （如 159960 网易 25,444.3600）—— 原文见 pcf_fetcher._parse_component_row
                #   的 `fixed_amount`。旧码在这里一律置 None ⇒ 引擎按 shares>0 取成分 ⇒
                #   整只成分漏出篮子 ⇒ IOPV 偏低、前端出现巨额假溢价（#15，9-17 定位）。
                #   ⚠️ 只对「数量=0 且货值>0」的行落值：「允许」类两列本就是 0.0000（= 无货值），
                #   置 None 而非 0，免得把「无货值」与「货值 0」混为一谈。
                cash_value = float(fixed_amt) if (vol <= 0 and fixed_amt and float(fixed_amt) > 0) else None
            comps.append({
                "stock_code": raw,
                "name": c.get("name"),
                "shares": vol,
                "cash_sub_flag": c.get("replace_flag"),
                "premium_ratio": c.get("replace_premium"),
                "discount_ratio": None,
                "subst_amount": subst,
                "cash_value": cash_value,
                "market": mkt,
            })
        return header, comps

    # ---- 解析 SSE XML ----
    def parse_xml(self, text):
        root = ET.fromstring(text)
        def g(tag):
            e = root.find(tag)
            return e.text if e is not None else None
        def gi(tag):
            v = g(tag)
            return int(v) if v not in (None, "") else None
        def gf(tag):
            v = g(tag)
            return float(v) if v not in (None, "") else None
        header = {
            "trade_date": g("TradingDay"),
            "pre_trade_date": g("PreTradingDay"),
            "units": gi("CreationRedemptionUnit"),
            "unit_nav": gf("NAVperCU"),
            "nav": gf("NAV"),
            "cash_diff": gf("PreCashComponent"),
            "est_cash_part": gf("EstimatedCashComponent"),
            "max_cash_ratio": gf("MaxCashRatio"),
            "creation_limit": gi("CreationLimit"),
            "redemption_limit": gi("RedemptionLimit"),
            # 额度三兄弟（可选字段，看 XML 原文，不可按市场推定）
            "creation_limit_per_acct": gi("CreationLimitPerAcct"),
            "net_creation_limit": gi("NetCreationLimit"),
            "net_creation_limit_per_acct": gi("NetCreationLimitPerAcct"),
            "record_number": gi("RecordNumber"),
        }
        comps = []
        cl = root.find("ComponentList")
        if cl is not None:
            for c in cl.findall("Component"):
                def cg(t):
                    e = c.find(t)
                    return e.text if e is not None else None
                comps.append({
                    "stock_code": cg("InstrumentID"),
                    "name": cg("InstrumentName"),
                    "shares": int(cg("Quantity")) if cg("Quantity") else None,
                    "cash_sub_flag": int(cg("SubstitutionFlag")) if cg("SubstitutionFlag") else None,
                    "premium_ratio": float(cg("CreationPremiumRate")) if cg("CreationPremiumRate") else None,
                    "discount_ratio": float(cg("RedemptionDiscountRate")) if cg("RedemptionDiscountRate") else None,
                    "subst_amount": float(cg("SubstitutionCashAmount")) if cg("SubstitutionCashAmount") else None,
                    "market": self._sse_market(cg("UnderlyingSecurityID")),
                })
        return header, comps

    # ---- 解析用户粘贴的表格文本 ----
    def parse_pasted_table(self, text):
        comps = []
        flag_re = "(?:允许|禁止|必须|现金替代|1|0)"
        patterns = [
            (re.compile(rf'^(\d{{5}})\s+(\S+)\s+{flag_re}\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\S+))?'), "ff"),
            (re.compile(rf'^(\d{{5}})\s+(\S+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\S+))?'), "sf"),
            (re.compile(rf'^(\d{{5}})\s+(\S+)\s+{flag_re}\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\S+))?'), "f"),
            (re.compile(rf'^(\d{{5}})\s+(\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\S+))?'), ""),
        ]
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for pat, kind in patterns:
                m = pat.match(line)
                if not m:
                    continue
                g = m.groups()
                if kind == "ff":
                    code, name, flag, shares, prem, disc, subst, market = g
                    comps.append(dict(stock_code=code, name=name, shares=int(shares),
                        cash_sub_flag=self._flag_to_int(flag), premium_ratio=float(prem),
                        discount_ratio=float(disc), subst_amount=float(subst),
                        market=self._norm_market(market)))
                elif kind == "sf":
                    code, name, shares, prem, disc, subst, market = g
                    comps.append(dict(stock_code=code, name=name, shares=int(shares),
                        cash_sub_flag=None, premium_ratio=float(prem),
                        discount_ratio=float(disc), subst_amount=float(subst),
                        market=self._norm_market(market)))
                elif kind == "f":
                    code, name, flag, prem, disc, subst, market = g
                    comps.append(dict(stock_code=code, name=name, shares=None,
                        cash_sub_flag=self._flag_to_int(flag), premium_ratio=float(prem),
                        discount_ratio=float(disc), subst_amount=float(subst),
                        market=self._norm_market(market)))
                else:
                    code, name, prem, disc, subst, market = g
                    comps.append(dict(stock_code=code, name=name, shares=None,
                        cash_sub_flag=None, premium_ratio=float(prem),
                        discount_ratio=float(disc), subst_amount=float(subst),
                        market=self._norm_market(market)))
                break
        return {"trade_date": None, "units": None}, comps

    @staticmethod
    def _flag_to_int(flag):
        if flag in ("允许", "现金替代", "1"):
            return 1
        if flag in ("禁止", "必须", "0"):
            return 0
        return None

    @staticmethod
    def _norm_market(m):
        if m is None:
            return "HK"
        if "港" in m or m.upper() in ("HK", "H"):
            return "HK"
        return m

    # 🔴 沪市 PCF `<UnderlyingSecurityID>` → 市场归属（2026-09-17 修正，docs/40 §2.1 / §七·4）
    #   普查 380 只沪市 ETF，该字段**全量取值只有** 101/102/103/106/9999（104 零命中）：
    #     101 → SH(上海)      102 → SZ(深圳)  ← 旧码把它当 HK（510500 的 220 只深市 A 股被误标）
    #     103/104 → HK(港股通) 106 → SH(上海)  9999 → OS(境外)
    #   语义红线：**只有 HK 才可能走逐券**（腾讯港股行情齐全），其余一律不按 HK 处理。
    #   未登记/空值**不猜**：空值返回 ''，未登记值原样返回（便于暴露 + 天然不等于 'HK'）。
    _SSE_MARKET = {"101": "SH", "102": "SZ", "103": "HK", "104": "HK", "106": "SH", "9999": "OS"}

    @staticmethod
    def _sse_market(uid):
        u = str(uid or "").strip()
        if not u:
            return ""
        return PCFFetcher._SSE_MARKET.get(u, u)

    # ---- 落库（按 trade_date upsert） ----
    def _persist(self, fund_code, header, comps, raw_text, source):
        trade_date = header.get("trade_date")
        if not trade_date:
            raise ValueError("trade_date 缺失，无法落库")
        c = conn(self.db)
        row = c.execute(
            "SELECT id FROM pcf_snapshot WHERE fund_code=? AND trade_date=?",
            (fund_code, trade_date)).fetchone()
        fetched_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if row:
            sid = row[0]
            c.execute("""UPDATE pcf_snapshot SET pre_trade_date=?, raw_text=?,
                cash_diff=?, est_cash_part=?, unit_nav=?, nav=?, units=?,
                max_cash_ratio=?, creation_limit=?, redemption_limit=?,
                creation_limit_per_acct=?, net_creation_limit=?,
                net_creation_limit_per_acct=?,
                record_number=?, source=?, fetched_at=? WHERE id=?""",
                (header.get("pre_trade_date"), raw_text, header.get("cash_diff"),
                 header.get("est_cash_part"), header.get("unit_nav"), header.get("nav"),
                 header.get("units"), header.get("max_cash_ratio"),
                 header.get("creation_limit"), header.get("redemption_limit"),
                 header.get("creation_limit_per_acct"), header.get("net_creation_limit"),
                 header.get("net_creation_limit_per_acct"),
                 header.get("record_number"), source, fetched_at, sid))
            # 🔴 仅在「新数据自带成分」时才重建：易方达 JSON 通道**不含成分**（comps==[]），
            #    若无条件 DELETE，会把同日已由官方 XML 落下的成分清空（9-16 实测踩到）。
            if comps:
                c.execute("DELETE FROM pcf_component WHERE snapshot_id=?", (sid,))
        else:
            c.execute("""INSERT INTO pcf_snapshot
                (fund_code,trade_date,pre_trade_date,raw_text,cash_diff,est_cash_part,
                 unit_nav,nav,units,max_cash_ratio,creation_limit,redemption_limit,
                 creation_limit_per_acct,net_creation_limit,net_creation_limit_per_acct,
                 record_number,source,fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fund_code, trade_date, header.get("pre_trade_date"), raw_text,
                 header.get("cash_diff"), header.get("est_cash_part"),
                 header.get("unit_nav"), header.get("nav"), header.get("units"),
                 header.get("max_cash_ratio"), header.get("creation_limit"),
                 header.get("redemption_limit"), header.get("creation_limit_per_acct"),
                 header.get("net_creation_limit"), header.get("net_creation_limit_per_acct"),
                 header.get("record_number"),
                 source, fetched_at))
            sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for cp in comps:
            c.execute("""INSERT INTO pcf_component
                (snapshot_id,fund_code,trade_date,stock_code,name,shares,
                 cash_sub_flag,premium_ratio,discount_ratio,subst_amount,cash_value,market)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, fund_code, trade_date, cp.get("stock_code"), cp.get("name"),
                 cp.get("shares"), cp.get("cash_sub_flag"), cp.get("premium_ratio"),
                 cp.get("discount_ratio"), cp.get("subst_amount"), cp.get("cash_value"),
                 cp.get("market")))
        c.commit()
        c.close()
        return sid, len(comps)

    # ---- 对外：按配置分派抓取；按内容分派解析 ----
    def _pcf_source(self, fund_code):
        """读配置的取数通道；无配置时按上交所兜底（保持旧行为）。"""
        try:
            c = sqlite3.connect(self.db)
            r = c.execute("SELECT pcf_source FROM fund_iopv_config WHERE fund_code=?",
                          (fund_code,)).fetchone()
            c.close()
            if r and r[0]:
                return str(r[0]).strip().lower()
        except Exception:
            pass
        return "sse_xml"

    def fetch_and_store(self, fund_code, date8=None):
        """按配置的 `pcf_source` 抓指定交易日（缺省=今天）的 PCF 并落库。

        · `sse_xml` ：上交所 query.sse.com.cn（接口只给**最新一份**，date8 无意义）
        · `szse_txt`：深交所静态 CDN 宽表，**可按日期回溯到上市首日**
        返回 (snapshot_id, 成分数)。取不到 → PCFUnavailable（不兜底）。"""
        src = self._pcf_source(fund_code)
        if src == "szse_txt":
            date8 = str(date8) if date8 else datetime.date.today().strftime("%Y%m%d")
            text, url = self.fetch_szse(fund_code, date8)
            header, comps = self.parse_szse(text, fund_code)
            return self._persist(fund_code, header, comps, text, url)
        if src == "efunds_json":
            date8 = str(date8) if date8 else datetime.date.today().strftime("%Y%m%d")
            return self.fetch_efunds_and_store(fund_code, date8)
        raw = self.fetch_sse(fund_code)
        header, comps = self.parse_xml(raw)
        return self._persist(fund_code, header, comps, raw, "sse_xml")

    # ---- 对外：自动判别 XML / 深交所宽表 / 粘贴表格 ----
    @staticmethod
    def _looks_like_szse_txt(text):
        """深交所宽表 txt 的稳定特征：『申购赎回清单』+『组合信息内容』。"""
        t = text or ""
        return ("申购赎回清单" in t) and ("组合信息内容" in t)

    def store(self, fund_code, text, source):
        if "<SSEPortfolioCompositionFile" in text or "<?xml" in text:
            header, comps = self.parse_xml(text)
        elif self._looks_like_szse_txt(text):
            header, comps = self.parse_szse(text, fund_code)
        else:
            header, comps = self.parse_pasted_table(text)
        return self._persist(fund_code, header, comps, text, source)

    def check_today_exists(self, fund_code):
        """今天是否已有 PCF 快照（不校验成分）。"""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        c = sqlite3.connect(self.db)
        row = c.execute(
            "SELECT trade_date FROM pcf_snapshot WHERE fund_code=? AND trade_date=?",
            (fund_code, today)).fetchone()
        c.close()
        return row is not None

    def check_today_complete(self, fund_code):
        """今天是否已有「完整」PCF：快照存在 **且** 成分已落库。

        必要性：只写快照不写成分（历史遗留/手工入库）会让 PCF 明细弹窗空窗，
        且导致昨天那天的篮子永远无法校准。"""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        c = sqlite3.connect(self.db)
        row = c.execute(
            "SELECT (SELECT COUNT(*) FROM pcf_snapshot WHERE fund_code=? AND trade_date=?),"
            "       (SELECT COUNT(*) FROM pcf_component WHERE fund_code=? AND trade_date=?)",
            (fund_code, today, fund_code, today)).fetchone()
        c.close()
        return bool(row and row[0] > 0 and row[1] > 0)

    def count_components(self, fund_code, announce_td):
        c = sqlite3.connect(self.db)
        n = c.execute("SELECT COUNT(*) FROM pcf_component WHERE fund_code=? AND trade_date=?",
                      (fund_code, announce_td)).fetchone()[0]
        c.close()
        return n

    # ---- seed 历史 PCF（来自已验证脚本 verify_iopv_513590_pcf.py） ----
    # ⚠️ 键位铁律（东哥 2026-09-14 钉死）：
    #     pcf_snapshot.trade_date     = 公告日 TradingDay
    #     pcf_snapshot.pre_trade_date = 信息内容日 PreTradingDay = 篮子定价基准日
    #   所以「基准 9-10 的篮子」属于 **9-11 公告**，「基准 9-11 的篮子」属于 **9-14 公告**。
    #   （早期 seed 把篮子按信息内容日挂，导致成分挂错公告 → 已于 2026-09-15 修正。）
    SEED_HEADERS = {
        # 公告日 -> (pre_trade_date, nav, cash_diff, est_cash_part, unit_nav)
        "20260910": ("20260909", 0.7564, 235.15, 105.21, 226933.47),
        "20260911": ("20260910", 0.7442, 324.58, -223.24, 223248.41),
        "20260914": ("20260911", 0.7425, -264.41, -206.15, 222740.97),
    }
    # 篮子基准日 -> 所属公告日（成分来源见 verify 脚本）
    SEED_BASKET_TO_ANNOUNCE = {"20260910": "20260911", "20260911": "20260914"}

    def seed_historical(self, fund_code="513590"):
        """补齐 seed 期公告头（9-10/9-11/9-14）与篮子成分。

        只 INSERT 缺失行，**不覆盖**已有数据（raw_text 解析结果优先）。返回写入的公告数。"""
        try:
            vp = os.path.join(HERE, "verify_iopv_513590_pcf.py")
            spec = importlib.util.spec_from_file_location("verify_mod", vp)
            vm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vm)
            baskets = {"20260910": list(vm.PCF_0910), "20260911": list(vm.PCF_0911)}
        except Exception as e:
            print("seed 跳过（无法加载 verify 脚本）:", e)
            return 0
        name_map = self._build_name_map(fund_code)
        c = conn(self.db)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = []
        # 各 seed 篮子的官方 Σ替代金额（用于识别「错挂」）
        basket_sum = {bd: round(sum(a for _, _, a in rows), 2) for bd, rows in baskets.items()}

        # ---- 1) 补齐公告头（只 INSERT 缺失，不覆盖） ----
        for ann_td, (pre_td, nav, cash, est, unit_nav) in self.SEED_HEADERS.items():
            row = c.execute("SELECT id FROM pcf_snapshot WHERE fund_code=? AND trade_date=?",
                            (fund_code, ann_td)).fetchone()
            if not row:
                c.execute(
                    "INSERT INTO pcf_snapshot (fund_code,trade_date,pre_trade_date,raw_text,"
                    "cash_diff,est_cash_part,unit_nav,nav,units,max_cash_ratio,"
                    "creation_limit,redemption_limit,record_number,source,fetched_at) "
                    "VALUES (?,?,?,NULL,?,?,?,?,300000,1.0,420000000,9000000,?,?,?)",
                    (fund_code, ann_td, pre_td, cash, est, unit_nav, nav, 50, "seed_script", now))
                inserted.append("公告" + ann_td + "(新建)")

        # ---- 2) 成分归位：只处理 **无 raw_text** 的公告（raw_text 解析结果最权威，不动） ----
        snaps = c.execute(
            "SELECT trade_date, pre_trade_date FROM pcf_snapshot "
            "WHERE fund_code=? AND (raw_text IS NULL OR raw_text='')", (fund_code,)).fetchall()
        for ann_td, pre_td in snaps:
            stored = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(subst_amount),0) FROM pcf_component "
                "WHERE fund_code=? AND trade_date=?", (fund_code, ann_td)).fetchone()
            n_cur, sum_cur = stored[0], round(stored[1] or 0.0, 2)
            target = baskets.get(pre_td)
            if target is not None:
                want = basket_sum[pre_td]
                if n_cur == 0 or abs(sum_cur - want) > 0.01:
                    c.execute("DELETE FROM pcf_component WHERE fund_code=? AND trade_date=?",
                              (fund_code, ann_td))
                    for (code, shares, subst) in target:
                        c.execute(
                            "INSERT INTO pcf_component (snapshot_id,fund_code,trade_date,stock_code,"
                            "name,shares,cash_sub_flag,premium_ratio,discount_ratio,subst_amount,market) "
                            "SELECT id,?,?,?,?,?,1,0.15,0.0,?,'HK' FROM pcf_snapshot "
                            "WHERE fund_code=? AND trade_date=?",
                            (fund_code, ann_td, code, name_map.get(code), shares, subst, fund_code, ann_td))
                    inserted.append("篮子%s→公告%s" % (pre_td, ann_td))
            elif n_cur and any(abs(sum_cur - s) <= 0.01 for s in basket_sum.values()):
                # 该公告的基准日没有 seed 篮子，但库里挂的却是别的篮子的成分 → 错挂，清掉
                c.execute("DELETE FROM pcf_component WHERE fund_code=? AND trade_date=?",
                          (fund_code, ann_td))
                inserted.append("清除错挂 公告%s(Σ=%.2f)" % (ann_td, sum_cur))
        c.commit()
        c.close()
        if inserted:
            print("seed 归位:", "; ".join(inserted))
        return len(self.SEED_HEADERS)

    # ---- 一键修复（幂等） ----
    def repair(self, fund_code="513590"):
        """修复数据完整性（幂等，可反复跑）：

        1) 对每个含 raw_text 的快照，按 raw_text 重新解析并重建成分
           —— 修复「只写快照、漏写成分」的历史遗留（如手工入库的 9-15）
        2) 补齐 seed 期（9-10/9-11/9-14 公告）的头部与篮子成分
        返回 (按 raw_text 重建的份数, seed 公告数)。
        """
        c = sqlite3.connect(self.db)
        rows = c.execute(
            "SELECT trade_date, raw_text, source FROM pcf_snapshot "
            "WHERE fund_code=? AND raw_text IS NOT NULL AND raw_text<>''", (fund_code,)).fetchall()
        c.close()
        n = 0
        for td, raw, src in rows:
            sid, cnt = self.store(fund_code, raw, src or "sse_xml")
            print(f"  raw_text 重建: 公告 {td} → snapshot_id={sid}, 成分 {cnt} 只")
            n += 1
        m = self.seed_historical(fund_code)
        return n, m

    def _build_name_map(self, fund_code):
        name_map = {}
        for path in [os.path.join(ROOT, "data", "pcf_513590_downloaded.bin")]:
            try:
                txt = open(path, "rb").read().decode("utf-8", "ignore")
                if "<SSEPortfolioCompositionFile" in txt:
                    _, comps = self.parse_xml(txt)
                    for cp in comps:
                        name_map[cp["stock_code"]] = cp["name"]
            except Exception:
                pass
        return name_map

    # ---- 展示 ----
    def show(self, fund_code="513590"):
        c = conn(self.db)
        rows = c.execute("""SELECT trade_date, units, unit_nav, nav, cash_diff,
            est_cash_part, record_number, source, fetched_at
            FROM pcf_snapshot WHERE fund_code=? ORDER BY trade_date""",
            (fund_code,)).fetchall()
        print(f"\n=== pcf_snapshot [{fund_code}] ({len(rows)} 条) ===")
        for r in rows:
            print(f"  {r[0]}  每份NAV={r[3]}  NAVperCU={r[2]}  现金差额={r[4]}  "
                  f"预估现金={r[5]}  成分数={r[6]}  来源={r[7]}  @ {r[8]}")
            cnt = c.execute("SELECT COUNT(*), SUM(shares) FROM pcf_component "
                           "WHERE fund_code=? AND trade_date=?", (fund_code, r[0])).fetchone()
            print(f"    └ 成分 {cnt[0]} 只, 总股数 {cnt[1]}")
        cfg = c.execute("SELECT * FROM fund_iopv_config WHERE fund_code=?", (fund_code,)).fetchone()
        if cfg:
            cols = [d[0] for d in c.execute("PRAGMA table_info(fund_iopv_config)")]
            print("\n=== fund_iopv_config ===")
            print("  ", dict(zip(cols, cfg)))
        c.close()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="P1: 上交所 PCF 数据模型 + 抓取/粘贴 + 落库")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--code", default="513590")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--date", type=str, default=None,
                    help="抓取指定交易日 YYYYMMDD（仅 szse_txt 通道有效；缺省=今天）")
    ap.add_argument("--paste", type=str, default=None)
    ap.add_argument("--seed-historical", action="store_true")
    ap.add_argument("--repair", action="store_true",
                    help="一键修复：按 raw_text 重建成分 + 补齐 seed 期篮子（幂等）")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    fetcher = PCFFetcher()

    if args.show:
        fetcher.show(args.code)
        return

    if not (args.init or args.seed_historical or args.fetch or args.paste or args.repair):
        # 默认：完整 P1 流水线（含修复，幂等）
        print(">>> 完整 P1 流水线")
        init_db(); print("[1] init_db 完成")
        n, m = fetcher.repair(args.code); print(f"[2] repair: raw_text 重建 {n} 份 / seed 公告 {m} 个")
        sid, cnt = fetcher.fetch_and_store(args.code)
        print(f"[3] fetch 最新 PCF: snapshot_id={sid}, 成分 {cnt} 只")
        fetcher.show(args.code)
        return

    if args.init:
        init_db(); print("init_db 完成")
    if args.repair:
        n, m = fetcher.repair(args.code)
        print(f"repair 完成: raw_text 重建 {n} 份 / seed 公告 {m} 个")
    if args.seed_historical:
        n = fetcher.seed_historical(args.code); print(f"seed 历史 PCF: {n} 条")
    if args.fetch:
        sid, cnt = fetcher.fetch_and_store(args.code, args.date)
        print(f"fetch+store snapshot_id={sid}, 成分 {cnt} 只")
    if args.paste:
        txt = open(args.paste, encoding="utf-8", errors="ignore").read()
        sid, cnt = fetcher.store(args.code, txt, "paste")
        print(f"paste+store snapshot_id={sid}, 成分 {cnt} 只")


if __name__ == "__main__":
    main()
