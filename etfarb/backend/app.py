# -*- coding: utf-8 -*-
"""
ETFarb 独立版 - 完整后端服务

架构：
  1. 数据采集层（scrapers/）- 从各网站获取数据
  2. 估值引擎（engine/）- 计算 IOPV、溢价、信号
  3. 后端服务（server/）- 提供 API 和看板
  4. QMT 策略（scripts/）- 仅负责下单（简化版）

职责分离：
  - 后端服务：每 3 秒扫描、计算、生成快照
  - QMT 策略：接收快照信号，执行下单（可选）
"""

from __future__ import print_function

import json
import os
import sys
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
import urllib.request

# 添加路径（跨平台自适应：Windows 本机 / ARM 部署同一份代码，无硬编码本机路径）
# 注入 LOFarb/src（arbcore 复用，与 ETFarb 同级），逻辑与 core/db.py:ensure_arbcore_on_path 一致：
#   ① ARBCORE_SRC / LOFARB_SRC 环境变量；② 逐级上溯找 <ARB>/LOFarb/src（core/db.py:find_sibling_path，
#      布局无关：ETFarb 位于 <ARB>/ETFarb/core 或 <ARB>/ETFarb/src/core 均可）；③ ARM /home/ubuntu/arbtest/src
_CORE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
import db as _db  # noqa: E402  —— core/db.py 导入时即注入 LOFarb/src 到 sys.path 并解析共享库
_db.ensure_arbcore_on_path()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============ 配置 ============
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
CORE_DIR = os.path.join(PROJ, "core")
WEB_DIR = os.path.join(PROJ, "frontend")
SNAPSHOT = os.path.join(PROJ, "data", "etf_snapshot.json")
LOG_DIR = os.path.join(PROJ, "log")
# ---- 看板缓存预热节拍（2026-10-08） ----
#   🔴 原 `time.sleep(20)` 是**固定间隔、不计入本轮装配耗时**：而 A 一次 build 要 130~200s
#   （docs/40 §四；2026-09-17 记录仅 3.6s），于是预热线程几乎从不喘息，叠加前台 SWR 后台
#   重建 ⇒ 多线程同时打同一批数据源 ⇒ 腾讯偶发抖动就被放大成「某 TAB 整列 price=None」。
#   改为**固定节拍**（耗时计入间隔）+ 至少睡 _PREWARM_MIN_SLEEP，慢市场跑多久就顺延多久。
_PREWARM_GAP = 60           # 两轮预热之间目标间隔（秒）
_PREWARM_MIN_SLEEP = 10     # 每轮结束至少休息多少秒（防止线程完全连轴转）
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
DEFAULT_PORT = 8102
# 监听地址：默认「仅本机」（纵深防御，2026-10-07）。nginx 反代走 127.0.0.1，无需对外监听；
# 如需局域网直连（如手机访问本机看板），设环境变量 ETFARB_BIND_HOST=0.0.0.0 即可恢复。
DEFAULT_BIND_HOST = os.environ.get("ETFARB_BIND_HOST", "127.0.0.1")

# 添加 core 目录到路径
sys.path.insert(0, CORE_DIR)

# ETFarb 独立数据层（core/db.py）：etfarb.db，与 arb_master.db 物理隔离，绝不影响 LOF 实盘
import db  # core/db.py
from db import get_conn, DB_PATH

# 通用静态估值引擎（指数比例法；ETF→基准配置见 core/static_valuation.py）
import static_valuation as sv
import pcf_fetcher as pcf

# 监控标的（阈值 th_p/th_d 为信号触发用，单位 bp）
WATCH = [
    ("510300.SH", "沪深300ETF", "国内宽基", 30, -30),
    ("159687.SZ", "亚太精选ETF南方", "跨境QDII-FoF", 50, -50),
    ("159329.SZ", "沙特ETF南方", "跨境QDII-单层", 30, -30),
]

# 限频状态
_log_state = {"last_err": None, "last_warn_t": 0.0, "gpcsp_cache": None, "gpcsp_time": 0,
              "last_collect_time": 0, "last_calib_date": None, "last_settle_date": None,
              "last_share_date": None}

# GPCSP 采集频率（秒）
_GPCSP_COLLECT_INTERVAL = 86400  # 每天一次（24小时）


# ============ 日志 ============

def _logf(msg):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except:
        pass


# ============ 数据获取层 ============

def _is_access_synced(sync_date, source_key):
    """检查今天是否已采集过该数据源"""
    try:
        conn = get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM access_sync_status WHERE sync_date=? AND access_source=?",
            (sync_date, source_key)
        )
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        _logf("检查防刷标志失败: " + str(e))
        return False


def _mark_access_synced(sync_date, source_key):
    """标记今日已采集"""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO access_sync_status (sync_date, access_source) VALUES (?, ?)",
            (sync_date, source_key)
        )
        conn.commit()
        conn.close()
        _logf(f"已标记 {source_key} 今日已采集")
    except Exception as e:
        _logf("标记防刷失败: " + str(e))


def _get_index_from_db(symbol):
    """从数据库读取某指数 symbol 最新 (close, date)。"""
    try:
        conn = get_conn()
        cursor = conn.execute(
            "SELECT date, close FROM index_history WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (symbol,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[1], row[0]
    except Exception as e:
        _logf("指数数据库读取失败 %s: %s" % (symbol, str(e)))
    return None, None


# 指数实时抓取通用能力（东财 push2 接口；数据中心IP对部分全局指数偶发限流，本机可用）
_INDEX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _http_get_json(url, headers=None, tries=2, timeout=2):
    """稳健 GET->JSON：先直连（绕过坏代理），失败再走环境代理；带温和退避。"""
    import urllib.request
    headers = headers or _INDEX_HEADERS
    last_err = None
    for attempt in range(tries):
        # 先直连（用户本机常配了失效 http_proxy 导致 ProxyError，故默认直连）
        for use_proxy in (False, True):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({} if not use_proxy else {})
            )
            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except Exception as e:
                last_err = e
                continue
        if attempt < tries - 1:
            time.sleep(0.5 + attempt * 0.5)  # 0.5s, 1s 退避
    _logf("HTTP 请求失败: " + str(last_err))
    return None


def _fetch_index_realtime(secid, scale=100):
    """东方财富实时接口取某指数最新点位（f43/scale）。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f43,f57,f58,f59,f60,f170,f171" % secid)
    d = _http_get_json(url)
    if d and d.get("data") and d["data"].get("f43"):
        idx = float(d["data"]["f43"]) / float(scale)
        prev = float(d["data"].get("f60") or 0) / float(scale)
        _logf("%s 实时获取成功: %.4f (昨收 %.4f)" % (secid, idx, prev))
        return idx, prev
    return None, None


def _fetch_index_realtime_tencent(qcode, scale=100):
    """腾讯实时接口取指数最新点位（qcode 如 'sh000300'，返回 price/prev）。"""
    url = "https://qt.gtimg.cn/q=%s" % qcode
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read().decode('gbk')
            parts = data.split('~')
            if len(parts) > 4 and parts[3]:
                price = float(parts[3])
                prev = float(parts[4] or 0)
                _logf("%s 腾讯实时获取成功: %.4f (昨收 %.4f)" % (qcode, price, prev))
                return price, prev
    except Exception as e:
        _logf("%s 腾讯实时获取失败: %s" % (qcode, str(e)))
    return None, None


def _save_index(close, symbol, date, source="eastmoney"):
    """将指数收盘写入 index_history（按 symbol+date 覆盖，真实值压过 manual）。"""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO index_history (symbol, date, close, source) VALUES (?, ?, ?, ?)",
            (symbol, date, float(close), source),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _logf("指数写入数据库失败 %s: %s" % (symbol, str(e)))


def _get_index_price(secid, symbol, scale=100):
    """获取某指数最新价格（防刷 + 失败节流 + 每日一次入库）。"""
    today = datetime.now().strftime('%Y-%m-%d')
    if _is_access_synced(today, symbol + "_index"):
        return _get_index_from_db(symbol)
    fail_t = _log_state.get(symbol + "_fail_t", 0)
    if time.time() - fail_t < 600:
        return _get_index_from_db(symbol)
    idx, prev = _fetch_index_realtime(secid, scale)
    if idx:
        _save_index(idx, symbol, today, "eastmoney")
        _mark_access_synced(today, symbol + "_index")
        return idx, today
    _log_state[symbol + "_fail_t"] = time.time()
    _logf("%s 实时获取失败，使用数据库缓存数据" % symbol)
    return _get_index_from_db(symbol)


# 向后兼容：GPCSP006 专用别名（旧调用方 _send_gpcsp / _daily_collect_loop 仍可用）
_GPCSP_SECID = "249.GPCSP006"
def _get_gpcsp_price():
    """获取 GPCSP006 最新指数价格（兼容旧调用方）。"""
    return _get_index_price(_GPCSP_SECID, "GPCSP006", 100)


def _secid_of(code):
    """'159687.SZ' -> '0.159687' ; '510300.SH' -> '1.510300'（深交所0/上交所1）。"""
    code_only, _, exch = code.partition('.')
    pfx = {"SZ": "0", "SH": "1", "OF": "0"}.get(exch.upper(), "0")
    return "%s.%s" % (pfx, code_only)


# 各 ETF f43 缩放：东财对低价标的(单价<2元)用 ×1000，普通 A股/ETF/指数用 ×100。
# 已实测核对：159687 实时价 1.812 → f43≈1812（×1000，非 ×100）；159329 实时价 0.92 → f43≈922（×1000）。
# 普通标的如 GPCSP006 指数 f43/100=2352.06（×100）。510300.SH 普通 ETF 用 ×100。
_ETF_SCALE = {"159329.SZ": 1000, "159687.SZ": 1000, "510300.SH": 100}


def _fetch_etf_realtime(secid, scale=100):
    """东方财富实时接口取 ETF 最新价（f43/scale）。"""
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f43,f57,f58,f60" % secid)
    d = _http_get_json(url)
    if d and d.get("data") and d["data"].get("f43"):
        price = float(d["data"]["f43"]) / float(scale)
        prev = float(d["data"].get("f60") or 0) / float(scale)
        _logf("%s ETF 实时获取成功: %.4f (昨收 %.4f)" % (secid, price, prev))
        return price, prev
    return None, None


def _fetch_etf_realtime_tencent(qcode, scale=100):
    """腾讯实时接口取 ETF 最新价（qcode 如 'sh510300'，返回 price/prev）。"""
    url = "https://qt.gtimg.cn/q=%s" % qcode
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read().decode('gbk')
            parts = data.split('~')
            if len(parts) > 4 and parts[3]:
                price = float(parts[3])
                prev = float(parts[4] or 0)
                _logf("%s 腾讯实时获取成功: %.4f (昨收 %.4f)" % (qcode, price, prev))
                return price, prev
    except Exception as e:
        _logf("%s 腾讯实时获取失败: %s" % (qcode, str(e)))
    return None, None


def _get_etf_price(code):
    """获取 ETF 实时价格（东财 push2 优先，失败回退腾讯/数据库 unified_fund_history.price）。"""
    db_code = code.split('.')[0]
    secid = _secid_of(code)
    scale = _ETF_SCALE.get(code, 100)
    today = datetime.now().strftime('%Y-%m-%d')
    # 当日已成功采过则直接读库（避免重复爬）
    if _is_access_synced(today, code + "_etf"):
        try:
            conn = get_conn()
            row = conn.execute(
                "SELECT price FROM unified_fund_history WHERE fund_code=? AND price IS NOT NULL ORDER BY date DESC LIMIT 1",
                (db_code,)).fetchone()
            conn.close()
            if row:
                return row[0]
        except:
            pass
    # 尝试东财接口
    price, _ = _fetch_etf_realtime(secid, scale)
    if not price:
        # 东财失败，一律尝试腾讯接口（所有 ETF/指数都可用）
        qcode = 'sh' + db_code if secid.startswith('1.') else 'sz' + db_code
        price, _ = _fetch_etf_realtime_tencent(qcode, scale)
    if price:
        _mark_access_synced(today, code + "_etf")
        # 把实时价回写 unified_fund_history 最新行（仅 ETFarb 自有标的；in_arbtest=1 归 arbTest，跳过）
        try:
            conn = get_conn()
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 price 是谁写的"（凌晨批来源排查教训）
            conn.execute(
                "UPDATE unified_fund_history SET price=?, writer='etfarb:app.realtime_price' "
                "WHERE fund_code=? AND date=(SELECT max(date) FROM unified_fund_history WHERE fund_code=?) "
                "AND fund_code NOT IN (SELECT fund_code FROM fund_profile WHERE in_arbtest=1)",
                (price, db_code, db_code))
            conn.commit()
            conn.close()
        except:
            pass
        return price
    # 实时失败：回退库里最近 price
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT price FROM unified_fund_history WHERE fund_code=? AND price IS NOT NULL ORDER BY date DESC LIMIT 1",
            (db_code,)).fetchone()
        conn.close()
        if row:
            return row[0]
    except:
        pass
    return None


def _get_fund_nav(code):
    """获取基金净值（从数据库）"""
    try:
        # 去掉 .SH/.SZ 后缀
        db_code = code.split('.')[0]
        conn = get_conn()
        cursor = conn.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL ORDER BY date DESC LIMIT 1",
            (db_code,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[1], row[0]
    except:
        pass
    return None, None


# 静态估值 / 实时 IOPV 已统一迁至 core/static_valuation.py（estimate_iopv / history_series）。
# 159687 的 GPCSP006 实时价仍由下方 _get_gpcsp_price 提供，喂给引擎 estimate_iopv。


# ============ PCF 清单元缓存 ============
_PCF_CACHE = {}  # {code_date: info_dict}
_PCF_TTL = 300   # 内存缓存 5 分钟；本地 json 每天只写一次


def _pcf_cache_key(code, date):
    return "%s_%s" % (code, date)


def _load_local_pcf(code, date):
    """读本地已缓存的 PCF json。返回包装 dict {'source_url': ..., 'pcf': ...}。"""
    fn = os.path.join(pcf.DATA_DIR, "pcf_%s_%s.json" % (code, date))
    if not os.path.exists(fn):
        return None
    try:
        with open(fn, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_pcf_info(code, date=None):
    """取 ETF 当日 PCF 结构化信息。仅深交所 ETF；上交所返回 None。
    优先本地缓存，没有则联网抓取一次并落盘。"""
    # 上交所暂不处理
    if code.endswith(".SH"):
        return None
    db_code = code.split('.')[0]
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    key = _pcf_cache_key(db_code, date)

    # 1) 内存缓存
    now = time.time()
    cached = _PCF_CACHE.get(key)
    if cached and now - cached.get("_t", 0) < _PCF_TTL:
        return cached.get("data")

    # 2) 本地 json
    wrapper = _load_local_pcf(db_code, date)
    if wrapper:
        info = wrapper.get("pcf")
        if info:
            info["source_url"] = wrapper.get("source_url", "")
            _PCF_CACHE[key] = {"data": info, "_t": now}
            return info

    # 3) 联网抓取（每天一次）
    try:
        text, src = pcf.fetch_pcf(db_code, date)
        if text:
            info = pcf.parse_pcf(text, db_code)
            pcf.save_pcf(info, date, src)
            info["source_url"] = src
            _PCF_CACHE[key] = {"data": info, "_t": now}
            _logf("PCF 抓取成功: %s %s -> %s" % (db_code, date, src))
            return info
    except Exception as e:
        _logf("PCF 抓取失败 %s %s: %s" % (db_code, date, str(e)))

    return None


# ============ 快照生成器 ============

def generate_snapshot():
    """生成快照数据（静态估值/实时 IOPV 统一走 core/static_valuation 引擎）"""
    etfs = []

    for code, name, kind, th_p, th_d in WATCH:
        db_code = code.split('.')[0]
        today = datetime.now().strftime('%Y-%m-%d')
        # 基础数据
        price = _get_etf_price(code)
        nav, nav_date = _get_fund_nav(code)

        # 主溢价：价格 vs 最新公告净值（盘后参考）
        premium_bps = round((price / nav - 1) * 10000, 1) if (price and nav) else None

        # 静态估值 / 实时 IOPV（统一引擎）
        static_val = None
        idx_price = None
        iopv_premium_bps = None
        iopv_reliable = True
        trustworthy = False
        note = ""
        cfg = sv.ETF_BENCHMARKS.get(db_code)
        if cfg:
            # 基准指数实时价：根据数据源选择获取方式
            secid = cfg.get("secid")
            data_source = cfg.get("data_source", "eastmoney")
            if secid:
                if data_source == "tencent":
                    # 腾讯接口：secid 格式为 'sh000300' 或 'sz399001'
                    idx_price, _ = _fetch_index_realtime_tencent(secid, cfg.get("idx_scale", 100))
                    if idx_price:
                        _save_index(idx_price, cfg["index"], today, "tencent")
                        _mark_access_synced(today, cfg["index"] + "_index")
                else:
                    idx_price, _ = _get_index_price(secid, cfg["index"], cfg.get("idx_scale", 100))
            if db_code == "159687":
                # 亚太精选 QDII：T-1 静态估值走 LCU 桥接（σ 83bp→4bp），不用纯指数法
                r = sv.estimate_t1_lcu(db_code)
            else:
                r = sv.estimate_iopv(db_code, idx_today=idx_price)
            static_val = r.get("static_val")
            trustworthy = r.get("trustworthy", False)
            note = r.get("note", "")
            # 盘中真实溢价：价格 vs IOPV（修正此前"溢价分母用错净值"的问题）
            if price and static_val:
                iopv_premium_bps = round((price / static_val - 1) * 10000, 1)
                # FoF 双层结构：IOPV 精度仅 ~116bp（σ），价格 vs IOPV 偏离在阈值内属噪声，
                # 不可据此判断瞬时套利；真实套利信号以 price/nav 溢价(premium_bps)为准。
                if cfg.get("structure") == "fof":
                    _noise = cfg.get("acceptable_sigma", 150)
                    if abs(iopv_premium_bps) < _noise:
                        iopv_reliable = False
                        note = "FoF IOPV 精度~116bp，小偏离为噪声；套利以 price/nav 溢价(premium_bps)为准"

        direction_base = iopv_premium_bps if iopv_premium_bps is not None else (premium_bps or 0)
        etf = {
            "code": code,
            "name": name,
            "kind": kind,
            "etf_price": round(price, 4) if price else None,
            "nav_t2": round(nav, 4) if nav else None,
            "premium_bps": premium_bps,
            "static_val": static_val,
            "iopv_premium_bps": iopv_premium_bps,
            "iopv_reliable": iopv_reliable,
            "benchmark": cfg["index"] if cfg else None,
            "idx_price": idx_price,
            "trustworthy": trustworthy,
            "direction": "premium" if direction_base > 0 else "discount",
            "note": note,
        }

        # 合并 PCF 清单元信息（最小申赎单位 / ECC / 申赎状态 / 成分替代比例）
        try:
            pcf_info = _get_pcf_info(code)
        except Exception as e:
            _logf("合并 PCF 信息失败 %s: %s" % (code, str(e)))
            pcf_info = None
        if pcf_info:
            components = pcf_info.get("components", [])
            real_components = [c for c in components if not c.get("is_virtual")]
            virtual_components = [c for c in components if c.get("is_virtual")]
            etf.update({
                "report_unit": pcf_info.get("report_unit"),
                "cash_balance": pcf_info.get("cash_balance"),
                "nav_per_cu": pcf_info.get("nav_per_cu"),
                "cash_replace_cap": pcf_info.get("cash_replace_cap"),
                "enable_creation": bool(pcf_info.get("enable_creation")),
                "enable_redemption": bool(pcf_info.get("enable_redemption")),
                "component_count": len(components),
                "real_component_count": len(real_components),
                "virtual_cash_amount": virtual_components[0].get("fixed_amount") if virtual_components else None,
                "components": components,
                "pcf_url": pcf_info.get("source_url", ""),
            })
        else:
            etf.update({
                "report_unit": None,
                "cash_balance": None,
                "nav_per_cu": None,
                "cash_replace_cap": None,
                "enable_creation": None,
                "enable_redemption": None,
                "component_count": None,
                "real_component_count": None,
                "virtual_cash_amount": None,
                "components": [],
                "pcf_url": "",
            })

        etfs.append(etf)

    # ---- 合并 IOPV 监控标的（fund_iopv_config enabled=1）到主看板 ----
    # 红线：缺数据返回 ERROR 行，不静默算错；与主看板其它标的同表展示。
    try:
        conn = get_conn()
        iopv_codes = [r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_iopv_config WHERE enabled=1").fetchall()]
        conn.close()
        seen = {e["code"] for e in etfs}
        for code in iopv_codes:
            if code in seen:
                continue
            row = _build_iopv_etf_row(code)
            if row:
                etfs.append(row)
    except Exception as e:
        _logf("合并 IOPV 标的到主看板失败: " + str(e))

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "etfs": etfs,
        "logs": []
    }


# ============ IOPV 引擎后端扩展（P4 / P6） ============
# 严格模式：实时计算缺数据一律返回 error，绝不兜底静默算错（东哥红线）。
# 模块加载失败不影响主看板（仅自建IOPV面板不可用）。
try:
    import iopv_calc as iopvmod
    from iopv_calc import IOPVCalculator, IOPVVerifier, IOPVCloseCalc, IOPVDataMissing
    import iopv_pcf as iopvpcf
    from iopv_pcf import PCFFetcher
    import fx_provider as fxp
    from fx_provider import FXProvider, FXMissingError
    _IOPV_OK = True
except Exception as _e:
    _logf("IOPV 引擎模块导入失败（不影响主看板）: " + str(_e))
    _IOPV_OK = False

_IOPV_CACHE = {}            # {code: {ts, data}} 内存缓存，减少外部行情请求
_IOPV_CACHE_TTL = 60        # 秒
_IOPV_ALERT_STATE = {}      # {code: last_triggered_bool} 状态翻转判定


def _init_iopv_alert_table():
    """新建 iopv_alert 表（状态翻转记录，便于历史追溯，不过度刷屏）。"""
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS iopv_alert(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code TEXT NOT NULL,
            ts TEXT,
            direction TEXT,        -- premium / discount
            premium_bps REAL,
            threshold_bps REAL,
            triggered INTEGER,      -- 1=触发告警 0=解除
            message TEXT
        )""")
        conn.commit()
        conn.close()
    except Exception as e:
        _logf("iopv_alert 建表失败: " + str(e))


def _is_trading_time():
    """第一性原理：盘后才不调实时接口、不产生错误价。
    A股 9:30-15:00 / 港股 9:30-16:00 → 取并集 9:15-16:00 工作日。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= hm <= 16 * 60


def _iopv_realtime(code, trade_date=None):
    """算实时 IOPV + 折溢价 + 超阈值判定。缺数据抛异常由调用方捕获。
    带内存缓存(60s)减少外部请求；非交易时段返回明确标记而非 stale 价。"""
    now = time.time()
    cached = _IOPV_CACHE.get(code)
    if cached and now - cached["ts"] < _IOPV_CACHE_TTL:
        return cached["data"]
    # 非交易时段：不拉实时行情，直接返回标记（不产生错误价）
    if not _is_trading_time():
        data = {"fund_code": code, "mode": "closed",
                "message": "非交易时段，实时 IOPV 不在盘中计算（不产生错误价）"}
        _IOPV_CACHE[code] = {"ts": now, "data": data}
        return data
    calc = IOPVCalculator(code)
    try:
        res = calc.calc_realtime(trade_date)
    except (IOPVDataMissing, FXMissingError) as _e:
        # 🔴 FoF / 跨市场 ETF（如 159687：LCU 新加坡 ETF 无实时盘口）自算 IOPV 不可行
        #   ⇒ 回退到腾讯官方 IOPV 溢价（f[77]/f[78]，深交所真值）做信号判据。
        #   仅报溢价（折价侧不碰，见 docs/analysis/30 §五）；basis 标 official_iopv 明示表观口径。
        return _iopv_realtime_official_fallback(code, _e)
    calc.persist_point(res)
    # ---- 实际折溢价（扣汇率楔子）：告警判据（2026-09-16 东哥定） ----
    # 🔴 为什么判它而不判表观 premium_bps：8~9 月港股通汇率楔子 −0.5%~−1.1%，
    #    表观口径会把真实溢价整段藏掉 —— 159366 实测 136 日表观溢价**从未突破 100bp**
    #    （最高 +0.77%），而同期真实（扣楔子）溢价 >50bp 的有 11 天。
    #    判表观 ⇒ 这条阈值线等于没装。
    # 🔴 缺 settle/mid 时**不退回表观**（否则阈值又形同虚设），而是不告警 + 写明原因。
    try:
        sfx = _get_settlement_fx(code)
        mid = _get_mid_fx_for_date(code, datetime.now().strftime('%Y-%m-%d'))
        pb_actual = _settle_premium_bps(res.get("market_price"), res.get("iopv"),
                                        sfx["rate"] if sfx else None,
                                        mid["rate"] if mid else None)
    except Exception:
        sfx, mid, pb_actual = None, None, None
    res["actual_premium_bps"] = pb_actual
    res["settle_rate"] = sfx["rate"] if sfx else None
    res["settle_rate_date"] = sfx["rate_date"] if sfx else None
    res["mid_rate"] = mid["rate"] if mid else None
    # 超阈值判定（用 per-ETF 阈值，UI 可配）
    pb = pb_actual
    p_th = res.get("premium_threshold_bps")
    d_th = res.get("discount_threshold_bps")
    alert = None
    if pb is None:
        alert = {"direction": "none", "over": False, "threshold_bps": p_th,
                 "premium_bps": None, "basis": "actual",
                 "reason": "缺结算汇率/中间价，无法判实际折溢价（不退回表观口径）"}
    elif p_th is not None and d_th is not None:
        if pb >= p_th:
            alert = {"direction": "premium", "over": True, "basis": "actual",
                     "threshold_bps": p_th, "premium_bps": pb}
        elif pb <= -d_th:
            alert = {"direction": "discount", "over": True, "basis": "actual",
                     "threshold_bps": d_th, "premium_bps": pb}
        else:
            alert = {"direction": "none", "over": False, "basis": "actual",
                     "threshold_bps": p_th, "premium_bps": pb}
    res["alert"] = alert
    # 状态翻转记录 iopv_alert（避免每轮询刷屏）
    triggered = bool(alert and alert["over"])
    prev = _IOPV_ALERT_STATE.get(code)
    if prev is None or prev != triggered:
        _IOPV_ALERT_STATE[code] = triggered
        try:
            conn = get_conn()
            conn.execute(
                "INSERT INTO iopv_alert(fund_code,ts,direction,premium_bps,threshold_bps,triggered,message) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 alert["direction"] if alert else "none", pb,
                 alert["threshold_bps"] if alert else p_th,
                 1 if triggered else 0,
                 (("实际溢价超阈值>" + str(p_th) + "bp") if alert and alert["direction"] == "premium"
                  else ("实际折价超阈值>" + str(d_th) + "bp") if alert and alert["direction"] == "discount"
                  else ("无法判实际折溢价: " + (alert.get("reason") or "")) if alert and alert.get("reason")
                  else "恢复至阈值内")))
            conn.commit()
            conn.close()
        except Exception as e:
            _logf("iopv_alert 写入失败: " + str(e))
    _IOPV_CACHE[code] = {"ts": now, "data": res}
    return res


def _iopv_realtime_official_fallback(code, exc):
    """自算 IOPV 不可信（FoF/跨市场缺成分行情）时的官方 IOPV 溢价回退信号。"""
    now = time.time()
    off = _official_iopv_proxy(code)
    if off is None:
        res = {"fund_code": code, "mode": "official_unavailable",
               "alert": {"direction": "none", "over": False, "basis": "official",
                         "reason": "自算IOPV缺失且官方IOPV取数失败: " + str(exc)}}
        _IOPV_CACHE[code] = {"ts": now, "data": res}
        return res
    pb = off["prem_pct"] * 100.0   # 官方 IOPV 溢价率（%→bp），表观口径
    p_th = _cfg_threshold(code, "premium")
    over = (p_th is not None and pb >= p_th)
    alert = {"direction": "premium" if over else "none", "over": over,
             "basis": "official_iopv", "threshold_bps": p_th, "premium_bps": pb,
             "reason": "自算IOPV不可信(LCU无行情)，改用官方IOPV溢价(表观口径)"}
    res = {"fund_code": code, "mode": "official_iopv_proxy",
           "iopv": off["iopv"], "market_price": off["price"],
           "official_iopv": off["iopv"], "official_iopv_prem": off["prem_pct"],
           "actual_premium_bps": pb, "alert": alert}
    _record_iopv_alert(code, alert, p_th)
    _IOPV_CACHE[code] = {"ts": now, "data": res}
    return res


def _official_iopv_proxy(code):
    """取腾讯官方 IOPV 溢价(f[77])/官方IOPV(f[78])/现价(f[3])，供 FoF 回退信号。"""
    try:
        import urllib.request
        prefix = "sh" if code.startswith(("5", "6")) else "sz"
        url = "https://qt.gtimg.cn/q=" + prefix + code
        raw = urllib.request.urlopen(url, timeout=5).read().decode("gbk", "ignore")
        parts = raw.split("~")
        if len(parts) < 79:
            return None
        return {"price": float(parts[3]), "prem_pct": float(parts[77]),
                "iopv": float(parts[78])}
    except Exception:
        return None


def _cfg_threshold(code, kind):
    """从 fund_iopv_config 取 per-ETF 阈值（bp）。"""
    try:
        c = get_conn()
        col = "premium_threshold_bps" if kind == "premium" else "discount_threshold_bps"
        v = c.execute("SELECT %s FROM fund_iopv_config WHERE fund_code=?" % col, (code,)).fetchone()
        c.close()
        return v[0] if v and v[0] is not None else None
    except Exception:
        return None


def _record_iopv_alert(code, alert, threshold):
    """状态翻转记录 iopv_alert（避免每轮询刷屏），与 _iopv_realtime 主流程共用。"""
    try:
        triggered = bool(alert and alert.get("over"))
        prev = _IOPV_ALERT_STATE.get(code)
        if prev is None or prev != triggered:
            _IOPV_ALERT_STATE[code] = triggered
            conn = get_conn()
            conn.execute(
                "INSERT INTO iopv_alert(fund_code,ts,direction,premium_bps,threshold_bps,triggered,message) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 alert.get("direction", "none"), alert.get("premium_bps"),
                 threshold, 1 if triggered else 0,
                 (("官方IOPV溢价超阈值>" + str(threshold) + "bp") if triggered
                  else "官方IOPV溢价恢复至阈值内")))
            conn.commit()
            conn.close()
    except Exception as e:
        _logf("iopv_alert 写入失败: " + str(e))


def _get_settlement_fx(code):
    """主看板用：今日(T) 可用的港股通「买入结算汇兑比率」= **上一交易日(T-1)** 的值。

    口径（东哥 2026-09-14 定 / 2026-09-15 钉死）：赎回 ETF → 基金卖出底层港股 →
    换汇行卖出人民币、买入港币 → 用「买入结算汇兑比率」。实际净值 = 公布净值 × (买入结算汇兑比率 / 中间价)。
    存于 fx_rate 表，ccy_pair = '{fx_currency}_SETTLE_BUY'（复用 CSV 灌入通道，零 schema 改动）。

    🔴 取日 = `rate_date < 今天`（适用日期 = T-1），**不是**「表里最新一条」：
    否则「适用日期 = T」的值一旦当天补喂入库，就会被当成 T 可用的汇率用，
    与列名「实际折溢价(T-1结算)」自相矛盾。未喂入 → 返回 None（前端显 待接入，绝不兜底造数）。

    ⚠️ 历史页**故意**不同口径：它取「适用日期 = D」的**当日真实值**（same_day=True）。
    两页差异是设计而非 bug —— 盘中事后回看，见 _get_settlement_fx_for_date 注释。"""
    return _get_settlement_fx_for_date(code, datetime.now().strftime('%Y-%m-%d'), same_day=False)


def _get_settlement_fx_for_date(code, trade_date, same_day=False):
    """取「交易日 D」的买入结算汇兑比率。**两套取日口径**（东哥 2026-09-15 拆开）：

    `fx_rate.rate_date` = **适用日期**。适用日 D 的结算汇率在 **D 当天盘中不公布**
    （上交所 D 日盘后 ~18:00 才发）→ 这在 T 日盘中看，就是"只能拿到 T-1 的值"。

      · `same_day=False`（默认，**主看板/实时口径**）：取 `rate_date < D` 最新一条
        = **T-1 值**。这是 T 日盘中真正**可得**的汇率，对应主看板列名
        「实际折溢价(T-1结算)」。绝不用 `<= D`，否则当天值一入库就会被错当成"今天可用的"。
      · `same_day=True`（**历史页/回看口径**）：取 `rate_date = D` **精确匹配**
        = T 日**真实**结算汇率（盘后已公布）。历史页是事后回看，用当日真值才反映
        那次套利**实际**发生的换汇成本，对应列名「实际折溢价(T结算)」。
        ⚠️ 不回退到 T-1：若当日值未喂入 → 返回 None（显「待接入」），
        否则列名写 T 却拿 T-1 的数，就是自相矛盾（2026-09-15 东哥抓的就是这个）。

    未命中 → None（前端显「待接入」，绝不兜底）。

    🔴 键位按**标的所在市场**选（2026-09-16 加）：深市标的(pcf_source=szse_txt)取
    `{ccy}_SETTLE_BUY_SZ`、沪市取 `..._SH`；没有则回退老键位 `{ccy}_SETTLE_BUY`
    （历史手填值都落在这里）。注意回退的是**同一日期的另一套报价**（差 ~1e-5），
    不是"回退到 T-1"—— 取日规则 `=` / `<` 不受影响。"""
    try:
        c = get_conn()
        cfg = c.execute(
            "SELECT fx_currency, pcf_source FROM fund_iopv_config WHERE fund_code=?", (code,)).fetchone()
        c.close()
        if not cfg or not cfg[0]:
            return None
        ccy = cfg[0]
        # 🔴 沪深两市「结算汇兑比率」是**两套报价**（9-15 实测：深 0.85519/0.85521、
        #    沪 0.85518/0.85522，中值相同、两脚差 ~1e-5）。按标的所在市场的行情源
        #    选键，再回退老键位（历史手填值都落在不带后缀的老键上）。
        mk = "SZ" if str(cfg[1] or "").lower() == "szse_txt" else "SH"
        keys = ["%s_SETTLE_BUY_%s" % (ccy, mk), "%s_SETTLE_BUY" % ccy]
        # trade_date 格式可能是 YYYYMMDD (8位) 或 YYYY-MM-DD (10位)，统一转 YYYY-MM-DD
        if len(trade_date) == 8:
            iso_date = trade_date[:4] + '-' + trade_date[4:6] + '-' + trade_date[6:8]
        else:
            iso_date = trade_date[:10]
        c = get_conn()
        op = "=" if same_day else "<"
        for k in keys:
            row = c.execute(
                "SELECT rate, rate_date FROM fx_rate WHERE ccy_pair=? AND rate_date " + op + " ? "
                "ORDER BY rate_date DESC LIMIT 1", (k, iso_date)).fetchone()
            if row:
                c.close()
                return {"rate": float(row[0]), "rate_date": row[1], "key": k}
        c.close()
        return None
    except Exception:
        return None


def _settle_premium_bps(mkt, iopv, settle_rate, mid_rate):
    """**主看板**「实际折溢价(T-1结算)%」：T 日盘中真正**可锁定**的折溢价（bp）。

    ```
    实际净值 = 当日 IOPV × 买入结算汇兑(T-1) ÷ 中间价(T)
    实际折溢价 = (市价 − 实际净值) ÷ 实际净值 × 10000
    等价式     = (1 + 实时折溢价) × mid ÷ settle − 1     ← 表观偏便宜 − 换汇贴水
    ```

    🔴 **分母是「当日 IOPV」，不是隔夜官方 nav_est**（东哥 2026-09-15 晚裁定，推翻旧式）。

    原因（与删「实际折溢价(中间价)」是同一个病）：`nav_est` 是 **T-1** 的官方净值，而市价是
    **T** 的 —— 不同日。拿隔夜净值当分母，等于把「T 日整篮子自己的涨跌」也算进折溢价里。
    实测 9-15：篮子当日 −1.146%，隔夜分母给出 **−1.29%**（看着像天大的折价套利机会），
    当日 IOPV 分母只有 **−0.16%**（真实空间 347 元 vs 假象 2,847 元）。

    ⚠️ 本函数**只用于主看板**。历史页「实际折溢价(T结算)%」是**同日**口径
    （`nav(D) × settle(D) ÷ mid(D)`，三个因子同一天），无跨日污染，**故意不共用本函数**。

    任一因子缺失 → None（前端显「待接入」，绝不兜底造数）。"""
    try:
        if not (mkt and iopv and settle_rate and mid_rate):
            return None
        actual_nav = float(iopv) * float(settle_rate) / float(mid_rate)
        if not actual_nav:
            return None
        return (float(mkt) - actual_nav) / actual_nav * 10000.0
    except Exception:
        return None


def _get_mid_fx_for_date(code, iso_date):
    """取指定日期的汇率中间价（fx_rate 表 ccy_pair = fx_currency，如 HKD/CNY）。

    取日规则（东哥 2026-09-15 钉死）：`mid` 取**当日**（A股交易日每天 9:20 公布）、
    `settle` 取**上一交易日**（当天不公布）→ 二者本就不同日，比值天然跨日，这不是错误。
    iso_date 为 'YYYY-MM-DD'；未命中 → 返回 None（该口径不显示，不兜底）。"""
    try:
        c = get_conn()
        cfg = c.execute(
            "SELECT fx_currency FROM fund_iopv_config WHERE fund_code=?", (code,)).fetchone()
        c.close()
        if not cfg or not cfg[0]:
            return None
        c = get_conn()
        row = c.execute(
            "SELECT rate, rate_date FROM fx_rate WHERE ccy_pair=? AND rate_date <= ? "
            "ORDER BY rate_date DESC LIMIT 1", (cfg[0], iso_date)).fetchone()
        c.close()
        if row:
            return {"rate": float(row[0]), "rate_date": row[1]}
        return None
    except Exception:
        return None


def _norm_date(d):
    """'20260911' -> '2026-09-11'；已是标准格式则原样返回。"""
    if not d:
        return None
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _build_iopv_etf_row(code):
    """把单只 IOPV 监控标的算成主看板统一行（含实时 IOPV 字段）。
    缺数据按红线返回 ERROR 状态行，绝不静默算错。"""
    try:
        conn = get_conn()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(fund_iopv_config)")]
        cfg_row = conn.execute("SELECT * FROM fund_iopv_config WHERE fund_code=?", (code,)).fetchone()
        conn.close()
        if not cfg_row:
            return None
        cfg = dict(zip(cols, cfg_row))
    except Exception:
        return None

    name = cfg.get("fund_name") or code
    # 类型（kind）也走配置：接入新标的只改 fund_iopv_config，前端零改动
    kind = cfg.get("kind") or "跨境QDII"
    units = cfg.get("creation_redemption_unit")

    base = {
        "code": code, "name": name, "kind": kind,
        "etf_price": None, "iopv": None, "premium_bps": None,
        "nav_est": None, "nav_date": None,
        "fx": None, "settlement_fx": None, "settlement_fx_date": None,
        "actual_premium_settle_bps": None,
        "computed_basket": None, "official_basket": None,
        "error_bps": None, "status": "ERROR",
        "report_unit": units, "create_limit": None, "redeem_limit": None,
        "pcf_url": "", "error": None,
        # 🔴 成分停牌（2026-09-17 立，见 docs/21 §二·F）：停牌券无成交价 ⇒ 官方 IOPV
        #   口径是沿用停牌前收盘价，我们照算；但停牌期间申赎替代款要等复牌后按实际
        #   成本结算 ⇒ 折溢价此刻没有兑现意义，必须在**标的层面**标「停牌中 · 暂停参考」。
        #   suspended_codes / suspended_weight 来自盘中实时行情口（mode=realtime）；
        #   休市时退回最近一份 iopv_close 的 WARN（suspended_from_db，附公告日）。
        "suspended_codes": [], "suspended_weight": None,
        "suspended_from_db": None, "suspended_date": None,
    }

    # 最新 verify（算法健康度）
    try:
        c = get_conn()
        v = c.execute(
            "SELECT trade_date, computed_basket, official_basket, error_bps, fx_used, status "
            "FROM iopv_verify WHERE fund_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        c.close()
        if v:
            base["official_basket"] = v[2]
            base["error_bps"] = v[3]
            base["verify_status"] = v[5]
            base["verify_date"] = v[0]
    except Exception:
        pass

    # 最新「已披露单位净值」(PCF 官方 NAV) → 净值Nav 列展示 + 日期备注。
    # PCF 的 NAV 字段是交易所官方披露每份额净值，优先使用；其次 unit_nav/units 推算值。
    try:
        c = get_conn()
        pc = c.execute(
            "SELECT trade_date, nav, unit_nav, units, pre_trade_date FROM pcf_snapshot "
            "WHERE fund_code=? AND (nav IS NOT NULL OR unit_nav IS NOT NULL) "
            "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        c.close()
        if pc:
            # 关键语义：PCF(T) 里的 NAV 字段是 T-1(PreTradingDay) 的已披露净值，
            # 不是 PCF 交易日 T 的净值（T 日净值要到 T+1 上午才公布）。
            # 因此净值日期须标注为 PreTradingDay，否则会把昨天的净值标成今天。
            nav_d = pc[4] if pc[4] is not None else pc[0]
            if pc[1] is not None and pc[1] > 0:
                # 官方 NAV（PCF NAV 字段）
                base["nav_est"] = float(pc[1])
                base["nav_date"] = _norm_date(nav_d)
            elif pc[2] is not None and pc[3] is not None and pc[3] > 0:
                # 回退：unit_nav / units
                base["nav_est"] = float(pc[2]) / float(pc[3])
                base["nav_date"] = _norm_date(nav_d)
            units = pc[3] if pc[3] else units
    except Exception:
        pass

    # 实时 IOPV（缺数据暴露错误，不兜底）
    try:
        res = _iopv_realtime(code)
    except (IOPVDataMissing, FXMissingError) as e:
        base["status"] = "ERROR"
        base["error"] = str(e)
        return base

    if res.get("error"):
        base["status"] = "ERROR"
        base["error"] = res.get("error")
        return base
    if res.get("mode") == "closed":
        # 休市：回填最近一个交易日的「已结算真实数据」，避免整行空白（非实时、标注休市，不伪造实时价）。
        # 来源：iopv_verify(篮子/误差) + pcf_snapshot(现金差额/预估现金→算 Nav/IOPV)
        #       + iopv_point(最近实时市价/折溢价) + fx_rate(最新中间价，不用 verify 历史 FX)
        base["status"] = "CLOSED"
        try:
            c = get_conn()
            v = c.execute(
                "SELECT trade_date, computed_basket, official_basket, error_bps, status "
                "FROM iopv_verify WHERE fund_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
            pc = c.execute(
                "SELECT cash_diff, est_cash_part, creation_limit, redemption_limit FROM pcf_snapshot "
                "WHERE fund_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
            pt = c.execute(
                "SELECT iopv, market_price, premium_bps FROM iopv_point "
                "WHERE fund_code=? ORDER BY ts DESC LIMIT 1", (code,)).fetchone()
            # FX 中间价：取 fx_rate 最新行（当天的 0.86325），不用 iopv_verify 的历史 FX
            fx_row = c.execute(
                "SELECT rate, rate_date FROM fx_rate "
                "WHERE ccy_pair=(SELECT fx_currency FROM fund_iopv_config WHERE fund_code=?) "
                "ORDER BY rate_date DESC LIMIT 1", (code,)).fetchone()
            # 成分停牌：休市拿不到实时行情 ⇒ 退回最近一份「已收盘 IOPV」的 WARN 记录
            #   （calc_one 对含停牌券的公告日落 status=WARN + note，见 core/iopv_calc.py）
            sc = c.execute(
                "SELECT trade_date, note FROM iopv_close WHERE fund_code=? AND status='WARN' "
                "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
            c.close()
            if sc:
                # 附上该 WARN 的公告日，避免把历史状态当成当前状态
                base["suspended_from_db"] = sc[1]
                base["suspended_date"] = _norm_date(sc[0])
            if v:
                base["computed_basket"] = v[1]
                base["official_basket"] = v[2]
                base["error_bps"] = v[3]
                base["verify_status"] = v[4]
                base["verify_date"] = v[0]
            if fx_row:
                base["fx"] = fx_row[0]
                # 不展示 fx_date，避免与 settle_date 混淆
            if pc and units and v and v[1] is not None:
                cash_diff = pc[0] if pc[0] is not None else 0.0
                est_cash = pc[1] if pc[1] is not None else 0.0
                # 仅当尚无官方 NAV 时，用 basket+cash_diff 估算（备用路径）
                if base.get("nav_est") is None:
                    base["nav_est"] = (v[1] + float(cash_diff)) / units
                # IOPV：优先使用 iopv_point 中已记录的盘中IOPV（更准确）
                # 仅在 iopv_point 无数据时才用篮子+预估现金重新计算
                if pt and pt[0] is not None:
                    base["iopv"] = pt[0]
                else:
                    base["iopv"] = (v[1] + float(est_cash)) / units
                # 申购赎回上限从最新PCF读取
                base["create_limit"] = pc[2] if pc[2] else None
                base["redeem_limit"] = pc[3] if pc[3] else None
            # 市价：优先最近实时点，退回最近收盘价
            mkt = pt[1] if (pt and pt[1] is not None) else None
            if mkt is None:
                try:
                    c2 = get_conn()
                    fn = c2.execute(
                        "SELECT price FROM unified_fund_history WHERE fund_code IN (?,?) "
                        "ORDER BY date DESC LIMIT 1", (code, code)).fetchone()
                    c2.close()
                    if fn and fn[0] is not None:
                        mkt = float(fn[0])
                except Exception:
                    pass
            base["etf_price"] = mkt
            # 折溢价（休市口径）：优先用最近实时点，否则 市价 vs 估算Nav
            if pt and pt[2] is not None:
                base["premium_bps"] = pt[2]
            elif mkt and base.get("nav_est"):
                base["premium_bps"] = ((mkt - base["nav_est"]) / base["nav_est"] * 10000)
            # （「实际折溢价(中间价)」已于 2026-09-15 删除：分母是隔夜官方净值，比当日市价无意义、盘中也骗人）
            # 结算汇兑比率（若已手动喂入）：赎回实际到账口径。
            # 取日：mid 取当日、settle 取上一交易日 T-1 —— 详见 _get_settlement_fx_for_date。
            sfx = _get_settlement_fx(code)
            if sfx:
                base["settlement_fx"] = sfx["rate"]
                base["settlement_fx_date"] = sfx["rate_date"]
                # 分母 = 当日 IOPV（不是隔夜 nav_est）—— 见 _settle_premium_bps 注释
                base["actual_premium_settle_bps"] = _settle_premium_bps(
                    mkt, base.get("iopv"), sfx["rate"], base.get("fx"))
        except Exception as e:
            _logf("CLOSED 回填失败 %s: %s" % (code, str(e)))
        return base

    mkt = res.get("market_price")
    base.update({
        "etf_price": mkt,
        "iopv": res.get("iopv"),
        "premium_bps": res.get("premium_bps"),
        "nav_est": base.get("nav_est"),        # 已披露单位净值(每份额)，见上方统一取数
        "fx": res.get("fx"),
        "settlement_fx": None,
        "settlement_fx_date": None,
        "actual_premium_settle_bps": None,
        "computed_basket": res.get("basket"),
        "status": base.get("verify_status") or "OK",
        "report_unit": units,
        # 成分停牌（盘中由实时行情口的交易状态字段判定，见 hk_quote.get_realtime_marks）
        "suspended_codes": res.get("suspended_codes") or [],
        "suspended_weight": res.get("suspended_weight"),
    })
    # 结算汇兑比率（买入，赎回实际到账汇率）：手动喂入则据其重算实际折溢价
    # 实际净值 = **当日 IOPV** × (结算买入价 T-1 / 当日中间价 T)，再算 (市价 - 实际净值)/实际净值
    # 取日（东哥 2026-09-15 钉死）：mid 取**当日**（每天 9:20 公布）、
    #   settle 取**上一交易日 T-1**（当天不公布）→ 比值天然跨日。
    # 分母（东哥 2026-09-15 晚裁定）：IOPV，**不用隔夜 nav_est** —— 见 _settle_premium_bps 注释。
    sfx = _get_settlement_fx(code)
    if sfx:
        base["settlement_fx"] = sfx["rate"]
        base["settlement_fx_date"] = sfx["rate_date"]
        base["actual_premium_settle_bps"] = _settle_premium_bps(
            mkt, base.get("iopv"), sfx["rate"], base.get("fx"))
    return base


# ============ 后端服务 ============

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        SimpleHTTPRequestHandler.__init__(self, *args, directory=WEB_DIR, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == "/api/snapshot" or p.startswith("/api/snapshot?"):
            self._send_snapshot()
        elif p == "/api/etf_market" or p.startswith("/api/etf_market?"):
            self._send_etf_market()
        elif p.startswith("/api/history/"):
            self._send_history()
        elif p == "/api/gpcsp":
            self._send_gpcsp()
        elif p.startswith("/api/iopv/"):
            self._send_iopv()
        elif p == "/api/pcf/view":
            self._send_pcf_view()
        elif p.startswith("/api/pcf/"):
            self._send_pcf()
        elif p.startswith("/api/fx/"):
            self._send_fx()
        elif p.startswith("/api/staticval/"):
            self._send_staticval()
        else:
            SimpleHTTPRequestHandler.do_GET(self)

    # ---- 跨境 ETF 看板（docs/40 §4.4）----
    #   GET /api/etf_market?market=E|A[&force=1]
    #   数据装配全在 core/etf_iopv_quote.ETFMarketBoard（一轮 = 一批行情 + 一次汇率
    #   + 指数缓存），后端只做 **30 秒缓存** 与 JSON 序列化，不算任何数字。
    #   ⚠️ 一轮耗时 3~6 秒（含网络），缓存是必需的；`force=1` 给「手动刷新」按钮。
    _MARKET_CACHE = {}          # market -> (ts, payload)
    _SWR_INFLIGHT = set()       # stale-while-revalidate 正在后台重建的 cache_key（防重复建线程）

    def _send_etf_market(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        market = (q.get("market", ["A"])[0] or "A").strip().upper()
        # 🔴 F=商品期货（东哥 10-09 拍板）：161226 白银 LOF / 159985 豆粕ETF / 159981 能源化工ETF。
        #    与 G（黄金原油）的界线 = **境内 vs 境外**：G 是境外油气黄金，F 是境内（上期/大商所/郑商所）商品期货。
        if market not in ("A", "E", "L", "C", "G", "F", "WATCH"):
            self._json(400, {"error": "market 只能是 A（亚洲）/ E（欧美）/ L（境内LOF）/ C（现金管理）/ G（黄金原油）/ F（商品期货）/ WATCH（关注池）"})
            return
        # 显示ETF / 显示LOF 复选框（前端「类型」过滤）
        fund_type = (q.get("type", [""])[0] or "").strip().upper()
        if fund_type and fund_type not in ("ETF", "LOF"):
            self._json(400, {"error": "type 只能是 ETF 或 LOF"})
            return
        force = (q.get("force", ["0"])[0] or "0") == "1"
        # 自选合并：前端把 localStorage 自选 code 列表用 codes= 传过来，
        # 与 market=WATCH 并集返回（关注池 ∪ 自选）。cache_key 必须含 codes，
        # 否则不同浏览器的自选会互相命中缓存。
        _raw_codes = (q.get("codes", [""])[0] or "").strip()
        codes = [c for c in _raw_codes.split(",") if c] if _raw_codes else None
        cache_key = market + (":" + fund_type if fund_type else "") + (":" + _raw_codes if _raw_codes else "")
        now = time.time()
        hit = Handler._MARKET_CACHE.get(cache_key)
        if hit and not force and (now - hit[0]) < 30:
            self._json(200, hit[1])
            return
        # stale-while-revalidate：ARM 跨境网络冷加载 ~47s，过期但有旧数据先返回，避免白屏。
        # 🔴 返旧的同时必须触发后台重建（2026-10-01 修）：否则非预热键（如 WATCH+codes，
        #    预热线程只刷 E/A/L/C 无 codes 键）会**永远冻结**在旧快照——watch_pool 删了基金，
        #    页面却一直显示旧并集。返旧即时 + 后台重建，下下次请求即是新数据。
        if hit and not force:
            if cache_key not in Handler._SWR_INFLIGHT:
                Handler._SWR_INFLIGHT.add(cache_key)
                def _rebuild(market=market, fund_type=fund_type, codes=codes, cache_key=cache_key):
                    try:
                        import etf_iopv_quote as eiq2
                        payload2 = eiq2.ETFMarketBoard(DB_PATH).build(
                            market=market, fund_type=fund_type or None, codes=codes)
                        Handler._MARKET_CACHE[cache_key] = (time.time(), payload2)
                    except Exception:
                        pass
                    finally:
                        Handler._SWR_INFLIGHT.discard(cache_key)
                threading.Thread(target=_rebuild, daemon=True).start()
            self._json(200, hit[1])
            return
        try:
            import etf_iopv_quote as eiq
            payload = eiq.ETFMarketBoard(DB_PATH).build(market=market,
                                                        fund_type=fund_type or None,
                                                        codes=codes)
        except Exception as e:
            _logf("etf_market 装配失败 %s:%s: %s" % (market, fund_type, e))
            self._json(500, {"error": str(e)})
            return
        Handler._MARKET_CACHE[cache_key] = (time.time(), payload)
        self._json(200, payload)

    # ---- 看板缓存预热（ARM 跨境网络慢，冷加载 ~47s；后台持续刷新保热）----
    # 配合上面 handler 的 stale-while-revalidate，访客永远能秒回（最多略旧），不再白屏。
    @classmethod
    def _prewarm_markets(cls):
        import etf_iopv_quote as eiq
        # 主看板 A 权重最高（每轮重建两次），覆盖全部交互视图（含类型过滤子集）
        # 🔴 2026-10-09 东哥拍板加 F（商品期货）：161226 白银走 lofarb_bridge 引用 LOFarb
        #   估值，F 市场仅 1 只、装配秒级（bridge 30s 缓存），但若不预热则 F 首屏冷加载会卡。
        order = [("A", None), ("E", None), ("G", None), ("A", "ETF"), ("L", None),
                 ("A", "LOF"), ("C", None), ("F", None), ("A", None)]
        i = 0
        while True:
            m, ft = order[i % len(order)]
            i += 1
            key = m + (":" + ft if ft else "")
            # 🔴 2026-10-08：跳过**前台正在后台重建（SWR）的同一 key**。否则预热线程与
            #   SWR 线程会并发装配同一个市场（A 一次 130~200s，叠起来就是双倍请求风暴；
            #   且撞上腾讯偶发抖动就会整列 price=None，见 core/etf_iopv_quote._qt_batch 事故回放）。
            if key in Handler._SWR_INFLIGHT:
                time.sleep(_PREWARM_GAP)
                continue
            t0 = time.time()
            try:
                payload = eiq.ETFMarketBoard(DB_PATH).build(market=m, fund_type=ft)
                cls._MARKET_CACHE[key] = (time.time(), payload)
            except Exception as e:
                _logf("etf_market 预热失败 %s:%s: %s" % (m, ft, e))
            # 固定节拍：本轮耗时计入间隔，慢市场跑多久就顺延多久（不再固定 sleep 20s）
            dt = time.time() - t0
            if dt > _PREWARM_GAP:
                _logf("预热 %s 单轮 %.1fs（>节拍 %ds，源端偏慢，见 docs/40 §四 build 耗时）"
                      % (key, dt, _PREWARM_GAP))
            time.sleep(max(_PREWARM_MIN_SLEEP, _PREWARM_GAP - dt))

    # ---- 通用 JSON 响应 ----
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- POST：PCF 抓取/粘贴、阈值配置 ----
    def do_POST(self):
        p = self.path.split('?')[0]
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(length).decode('utf-8', 'ignore') if length else ''
        except Exception:
            raw = ''
        if p.startswith("/api/pcf/fetch/"):
            code = p.strip('/').split('/')[3]
            try:
                sid, cnt = PCFFetcher().fetch_and_store(code)
                # 抓到新公告后立刻补齐篮子校准（前一日篮子此刻才具备「成分+收盘+汇率」）
                _calibrate_pending_pcf()
                self._json(200, {"ok": True, "snapshot_id": sid, "components": cnt})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if p.startswith("/api/pcf/paste/"):
            code = p.strip('/').split('/')[3]
            try:
                body = json.loads(raw) if raw else {}
                text = body.get("text", "")
                if not text:
                    self._json(400, {"ok": False, "error": "缺 text"})
                    return
                sid, cnt = PCFFetcher().store(code, text, "paste_ui")
                self._json(200, {"ok": True, "snapshot_id": sid, "components": cnt})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if p.startswith("/api/iopv/config/"):
            code = p.strip('/').split('/')[3]
            try:
                body = json.loads(raw) if raw else {}
                sets, vals = [], []
                if body.get("premium_threshold_bps") is not None:
                    sets.append("premium_threshold_bps=?"); vals.append(int(body["premium_threshold_bps"]))
                if body.get("discount_threshold_bps") is not None:
                    sets.append("discount_threshold_bps=?"); vals.append(int(body["discount_threshold_bps"]))
                if body.get("fx_currency") is not None:
                    sets.append("fx_currency=?"); vals.append(body["fx_currency"])
                if body.get("fx_rate") is not None:
                    sets.append("fx_rate=?"); vals.append(float(body["fx_rate"]))
                if not sets:
                    self._json(400, {"ok": False, "error": "无更新字段"})
                    return
                vals.append(code)
                conn = get_conn()
                conn.execute("UPDATE fund_iopv_config SET " + ", ".join(sets) + " WHERE fund_code=?", vals)
                conn.commit(); conn.close()
                self._json(200, {"ok": True, "updated": body})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(404, {"error": "未知 POST 路由: " + p})

    # ---- DELETE：取消「我的关注」（含 AI 喂入的库内基金，用户可随时退出）----
    def do_DELETE(self):
        p = self.path.split('?')[0]
        if p.startswith("/api/watch/"):
            parts = p.strip('/').split('/')
            code = parts[2] if len(parts) > 2 else ''
            if not (code and code.isdigit() and len(code) == 6):
                self._json(400, {"ok": False, "error": "code 非法（需 6 位数字）"})
                return
            try:
                conn = get_conn()
                conn.execute("DELETE FROM watch_pool WHERE code=?", (code,))
                conn.commit(); conn.close()
                # 🔴 watch_pool 变了 ⇒ 所有市场行的 watched 标志都会变，
                #    全清行情缓存（含 WATCH+codes 键），否则 stale-while-revalidate 会一直返旧
                Handler._MARKET_CACHE.clear()
                self._json(200, {"ok": True, "code": code, "deleted": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    # ---- 历史核对页（东哥 09-19：UI 完全对齐 LOF 套利程序——arbTest 历史弹窗）----
    # kind='cash' 债券三只 / 'lof'；refresh=1 =「核对静态估值（补采+重算）」按钮
    def _send_staticval(self):
        seg = self.path.strip('/').split('/')   # /api/staticval/history/<code>
        code = seg[3] if len(seg) >= 4 else None
        if code:
            code = code.split("?", 1)[0]         # 去掉可能的查询串（?refresh=1）
        refresh = "refresh=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
        if not code:
            self._json(400, {"error": "缺基金代码"})
            return
        try:
            import fund_history as _fh
            # days=180：覆盖 fund_nav_history/fund_share 全历史（2026-01 起 ~178 行），
            # 前端按 size 分页（默认 10 条/页）。原 days=60 会截掉早期份额行（深市
            # 份额 2026-07-06 起 ~59 交易日，与净值 178 行日期错位后易被截）。
            d = _fh.get_history(code, days=180, refresh=refresh, db=DB_PATH)
            if not d:
                self._json(404, {"error": "未知基金 " + code})
                return
            self._json(200, d)
        except Exception as e:
            _logf("staticval API 异常: " + str(e))
            self._json(500, {"error": str(e)})

    # ---- IOPV 路由分发 ----
    def _send_iopv(self):
        if not _IOPV_OK:
            self._json(503, {"error": "IOPV 模块未加载（不影响主看板）", "type": "module_missing"})
            return
        seg = self.path.strip('/').split('/')[2:]   # 去掉 'api/iopv'
        try:
            if seg and seg[0] == "alerts":
                self._json(200, self._iopv_alerts())
                return
            if seg and seg[0] == "verify" and len(seg) >= 3:
                cached = self._iopv_verify_cached(seg[1], seg[2])
                if cached:
                    self._json(200, cached)
                    return
                r = IOPVVerifier(seg[1]).verify_trade_date(seg[2])
                self._json(200, r)
                return
            if seg and seg[0] == "history" and len(seg) >= 2:
                self._json(200, self._iopv_history(seg[1]))
                return
            if seg and seg[0] == "config" and len(seg) >= 2:
                self._json(200, self._iopv_config(seg[1]))
                return
            if seg and seg[0] == "fx" and len(seg) >= 2:
                self._json(200, self._iopv_fx(seg[1]))
                return
            if seg and seg[0] == "detail" and len(seg) >= 2:
                self._json(200, self._iopv_detail(seg[1]))
                return
            if len(seg) == 1:
                # /api/iopv/<code> 实时
                self._json(200, self._iopv_realtime_safe(seg[0]))
                return
            self._json(404, {"error": "未知 IOPV 路由", "path": self.path})
        except (IOPVDataMissing, FXMissingError) as e:
            # 红线：缺数据暴露错误，绝不兜底
            self._json(200, {"error": str(e), "type": type(e).__name__, "fund_code": seg[-1] if seg else None})
        except Exception as e:
            _logf("IOPV API 异常: " + str(e))
            self._json(500, {"error": str(e), "type": "server_error"})

    def _iopv_realtime_safe(self, code):
        return _iopv_realtime(code)

    def _iopv_verify_cached(self, code, date):
        try:
            c = get_conn()
            row = c.execute(
                "SELECT trade_date, computed_basket, official_basket, error_bps, fx_used, status "
                "FROM iopv_verify WHERE fund_code=? AND trade_date=?", (code, date)).fetchone()
            c.close()
            if not row:
                return None
            return {"trade_date": row[0], "computed_basket": row[1], "official_basket": row[2],
                    "error_bps": row[3], "fx_used": row[4], "status": row[5], "cached": True}
        except Exception:
            return None

    def _iopv_alerts(self):
        c = get_conn()
        codes = [r[0] for r in c.execute(
            "SELECT fund_code FROM fund_iopv_config WHERE enabled=1")]
        c.close()
        alerts = []
        for code in codes:
            try:
                res = _iopv_realtime(code)
                if res.get("mode") == "closed":
                    continue
                a = res.get("alert")
                if a and a.get("over"):
                    alerts.append({
                        "fund_code": code, "direction": a["direction"],
                        "premium_bps": a["premium_bps"],
                        "threshold_bps": a["threshold_bps"],
                        "iopv": res.get("iopv"),
                        "market_price": res.get("market_price"),
                    })
            except (IOPVDataMissing, FXMissingError) as e:
                alerts.append({"fund_code": code, "error": str(e), "type": type(e).__name__})
        return {"alerts": alerts,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def _iopv_history(self, code):
        c = get_conn()
        points = c.execute(
            "SELECT ts, trade_date, iopv, market_price, premium_bps FROM iopv_point "
            "WHERE fund_code=? ORDER BY id DESC LIMIT 300", (code,)).fetchall()
        verifies = c.execute(
            "SELECT trade_date, computed_basket, official_basket, error_bps, fx_used, status "
            "FROM iopv_verify WHERE fund_code=? ORDER BY trade_date", (code,)).fetchall()
        c.close()
        return {
            "fund_code": code,
            "points": [{"ts": r[0], "trade_date": r[1], "iopv": r[2],
                        "market_price": r[3], "premium_bps": r[4]} for r in points],
            "verifies": [{"trade_date": r[0], "computed_basket": r[1],
                          "official_basket": r[2], "error_bps": r[3],
                          "fx_used": r[4], "status": r[5]} for r in verifies],
        }

    def _iopv_config(self, code):
        c = get_conn()
        row = c.execute("SELECT * FROM fund_iopv_config WHERE fund_code=?", (code,)).fetchone()
        cols = [d[0] for d in c.execute("PRAGMA table_info(fund_iopv_config)")]
        c.close()
        return {"config": dict(zip(cols, row))} if row else {"config": None}

    def _iopv_detail(self, code):
        """历史明细（主看板点击行弹出）：头部静态信息 + 按日期降序的逐日校准行。"""
        def _iso(td):
            return "%s-%s-%s" % (td[:4], td[4:6], td[6:]) if td and len(td) == 8 else td

        c = get_conn()
        cfg = c.execute(
            "SELECT fund_name, creation_redemption_unit, fx_currency FROM fund_iopv_config WHERE fund_code=?",
            (code,)).fetchone()
        snap = c.execute(
            "SELECT trade_date, units, cash_diff, est_cash_part, creation_limit, redemption_limit FROM pcf_snapshot "
            "WHERE fund_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        units = snap[1] if snap else (cfg[1] if cfg else None)
        # 申购赎回上限统一用最新PCF的值
        latest_create_limit = snap[4] if snap and snap[4] else None
        latest_redeem_limit = snap[5] if snap and snap[5] else None

        # 行来源（2026-09-15 修 Bug A）：
        #   所有已知交易日 = pcf_snapshot.trade_date(公告日) ∪ pre_trade_date(篮子基准日)，降序。
        #   ← 旧实现以 iopv_verify 为行来源，于是「有 PCF 但没跑过校准」的日期整行消失，
        #     表现为点 513590 弹窗里只有 9-10/9-11、看不到 9-14。
        days = sorted({r[0] for r in c.execute(
            "SELECT trade_date FROM pcf_snapshot WHERE fund_code=? "
            "UNION SELECT pre_trade_date FROM pcf_snapshot WHERE fund_code=? "
            "AND pre_trade_date IS NOT NULL", (code, code)).fetchall() if r[0]}, reverse=True)
        # 校准结果：iopv_verify.trade_date = 篮子基准日 D（不是公告日）
        verifies = {r[0]: r for r in c.execute(
            "SELECT trade_date, computed_basket, official_basket, error_bps, fx_used, status, note "
            "FROM iopv_verify WHERE fund_code=?", (code,)).fetchall()}
        # 收盘 IOPV：iopv_close.trade_date = 公告日 T（= 本行的交易日）
        #   = (Σ(T公告成分 × T日港股不复权收盘 × T日中间价) + T公告预估现金) ÷ 申赎单位
        closes = {r[0]: r for r in c.execute(
            "SELECT trade_date, iopv, basket, est_cash, status, note "
            "FROM iopv_close WHERE fund_code=?", (code,)).fetchall()}

        # 买入结算汇兑比率（手动喂入的 T-1 值）：历史行按 trade_date 取对应日期的值
        rows = []
        for td in days:
            v = verifies.get(td)
            comp = v[1] if v else None
            off = v[2] if v else None
            err = v[3] if v else None
            st = v[5] if v else "PENDING"
            vnote = v[6] if v else "未校准：需该日的下一份 PCF 成分 + 该日港股收盘"
            # 该篮子的定价汇率 = 该交易日的 HKD/CNY 中间价（有校准行则用它的 fx_used，同源同值）
            mid_fx = _get_mid_fx_for_date(code, _iso(td))
            fx = (v[4] if (v and v[4]) else None) or (mid_fx["rate"] if mid_fx else None)
            # 口径（2026-09-14 东哥钉死）：一份 PCF 公告同时含两段——
            #   · T-1 段：nav / cash_diff / unit_nav，归属「前一交易日」(pre_trade_date)
            #   · T   段：est_cash_part（预估现金差额），归属「公告日」(trade_date)
            # 对历史行 D：
            #   · 当日净值/现金差额 → 来自「下一份 PCF」D+1 公告（pre_trade_date = D）
            #   · 当日预估现金差额 → 来自「当日 PCF」D 公告（trade_date = D）
            pn = c.execute(
                "SELECT trade_date, nav, cash_diff, units, unit_nav, est_cash_part FROM pcf_snapshot "
                "WHERE fund_code=? AND pre_trade_date=?", (code, td)).fetchone()
            pe = c.execute(
                "SELECT trade_date, est_cash_part FROM pcf_snapshot "
                "WHERE fund_code=? AND trade_date=?", (code, td)).fetchone()
            pcf_nav = pn[1] if (pn and pn[1]) else None
            cash_diff = pn[2] if (pn and pn[2] is not None) else None
            # 最小申购、赎回单位资产净值（PCF 的 NAVperCU，属 T-1 段，出自 D+1 公告）
            unit_nav_val = pn[4] if (pn and pn[4] is not None) else None
            est = pe[1] if pe else None
            # 提供 nav/现金差额 的那份公告日（PCF 的 TradingDay，= D+1）
            pcf_announce_date = _iso(pn[0]) if pn else None
            # 预估现金所属公告日（PCF 的 TradingDay，= D）
            est_cash_date = _iso(pe[0]) if pe else None
            create_limit = latest_create_limit
            redeem_limit = latest_redeem_limit
            pr = c.execute("SELECT price FROM unified_fund_history WHERE fund_code=? AND date=?",
                           (code, _iso(td))).fetchone()
            mkt = pr[0] if pr else None
            if mkt is None:
                # fund_nav 未覆盖的日期（如 9-14 起东财断更）：回退到 iopv_point 当日最后一个采样市价
                ip = c.execute(
                    "SELECT market_price FROM iopv_point WHERE fund_code=? AND trade_date=? "
                    "AND market_price IS NOT NULL ORDER BY ts DESC LIMIT 1", (code, td)).fetchone()
                mkt = ip[0] if ip else None
            # 🔴 2026-09-17 东哥定：列名既为「ETF收盘价」，就只有**已收盘**的交易日才有值。
            #   当天行（D ≥ 今天）收盘价尚未产生 ⇒ 留空，与「收盘IOPV」列**同闸门**
            #   （iopv_close 侧为 `公告日 ≥ 今天 → 跳过`，见 iopv_calc.py calc_all），
            #   两列次日一起补出。否则 iopv_point 的末次采样是**盘中价**，会被当收盘价显示；
            #   fund_nav 若被盘中 etf_quote 回写了当日行，同样要拦 —— 故判定在两级来源之外。
            #   ⚠️ 只按行日期判定、不看 iopv —— 停牌券致 iopv_close 缺失的**历史**日不受影响
            #      （那几天收盘价照常显示，见 159366 9-01~9-15）。
            if _iso(td) >= datetime.now().strftime("%Y-%m-%d"):
                mkt = None
            # 该交易日 D 的结算汇率：历史页用 **D 日真实值**（same_day=True，盘后已公布），
            # 与主看板的 T-1 口径**故意不同** —— 主看板是 T 日盘中"可得"的值（只能 T-1），
            # 历史页是事后回看，用它当天真实发生的换汇成本。列名分别为 (T结算) / (T-1结算)。
            sfx = _get_settlement_fx_for_date(code, td, same_day=True)
            # IOPV（2026-09-15 东哥定口径）：取 iopv_close[trade_date=D]
            #   = [ Σ(D公告成分股数 × D日港股不复权收盘 × D日中间价) + D公告预估现金 ] ÷ 申赎单位
            #   · 与主看板实时 IOPV 同口径（收盘时刻两值相等）；篮子与预估现金同源，无跨日拼接。
            #   · D 当天盘后即可算，不必等 D+1 公告；取不到就显「—」，绝不兜底。
            # ⚠️ 旧实现 = (verify(D).篮子 + D+1公告预估现金) / units —— 那是 D 的官方净值，
            #    不是 IOPV（使「收盘IOPV」（旧称「实时IOPV」）≡「净值Nav」，并有 ≤1.2bp 跨日拼接误差）。已废弃。
            ic = closes.get(td)
            iopv = ic[1] if (ic and ic[1] is not None) else None
            iopv_basket = ic[2] if ic else None
            iopv_status = ic[4] if ic else None
            iopv_note = ic[5] if ic else None
            # nav_est：当日净值 = 下一份 PCF(D+1) 的官方 NAV；D+1 公告未发布前为 None（显示「待公布」）
            nav_est = pcf_nav
            prem = ((mkt - iopv) / iopv * 10000) if (mkt and iopv) else None
            # 结算口径实际折溢价（T结算）：实际净值 = 官方 nav(D) × 结算买入价(D) / 中间价(D)
            # 🔴 分母用**官方 nav(D)**（不是 IOPV，也不共用主看板的 _settle_premium_bps）：
            #    本页是事后回看，mkt/nav/settle/mid **四个因子全是 D 日同一天**，没有跨日污染，
            #    且回看就该用官方净值（权威）而非自建估值。主看板盘中拿不到 nav(T)，才改用当日 IOPV。
            #    两页同名不同源是**设计**，不是 bug（2026-09-15 东哥拆开）。
            act_settle = None
            if sfx and mkt and nav_est and fx:
                actual_nav = nav_est * sfx["rate"] / fx
                act_settle = ((mkt - actual_nav) / actual_nav * 10000)
            rows.append({
                "date": _iso(td),
                "etf_price": mkt,
                "iopv": iopv,
                # 收盘 IOPV 的明细（供 tooltip / 排错）：篮子与预估现金同源（同一份 D 公告）
                "iopv_basket": iopv_basket,
                "iopv_status": iopv_status,
                "iopv_note": iopv_note,
                "premium_bps": prem,
                "nav_est": nav_est,
                # 净值基准日 = 该交易日 D 本身（NAV(D) 出自 D+1 公告的 T-1 段）
                "nav_date": _iso(td),
                # PCF 原始数据（均归属该交易日 D）
                "cash_diff": cash_diff,
                "est_cash": est,
                "pcf_trade_date": pcf_announce_date,
                "est_cash_date": est_cash_date,
                "actual_premium_settle_bps": act_settle,
                "fx": fx,
                "settlement_fx": sfx["rate"] if sfx else None,
                "settlement_fx_date": sfx["rate_date"] if sfx else None,
                "computed_basket": comp,
                "official_basket": off,
                "unit_nav": unit_nav_val,
                "error_bps": err,
                "status": st,
                "verify_note": vnote,
                "create_limit": create_limit,
                "redeem_limit": redeem_limit,
            })
        c.close()
        return {
            "fund_code": code,
            "name": cfg[0] if cfg else code,
            "header": {
                "report_unit": units,
                "create_limit": latest_create_limit,
                "redeem_limit": latest_redeem_limit,
                "fx_currency": cfg[2] if cfg else None,
                "settlement_fx": rows[0]["settlement_fx"] if rows else None,
                "settlement_fx_date": rows[0]["settlement_fx_date"] if rows else None,
            },
            "rows": rows,
        }

    def _iopv_fx(self, code):
        if not _IOPV_OK:
            return {"error": "FX 模块未加载", "type": "module_missing"}
        try:
            fx = FXProvider(code)
            info = fx.get_rate_info()
            c = get_conn()
            rows = c.execute(
                "SELECT rate_date, rate, source FROM fx_rate WHERE ccy_pair=? "
                "ORDER BY rate_date DESC LIMIT 10", (fx.ccy_pair,)).fetchall()
            c.close()
            return {"ccy_pair": fx.ccy_pair, "latest": info,
                    "history": [{"date": r[0], "rate": r[1], "source": r[2]} for r in rows]}
        except FXMissingError as e:
            return {"error": str(e), "type": "FXMissingError"}

    # ---- PCF 只读查询 ----
    # ---- 交易所 PCF 原文件「可读化」页 ----
    #   GET /api/pcf/view?code=513500          → 可读 HTML（沪市 XML 渲染成字段表 + 成分表）
    #   GET /api/pcf/view?code=513500&raw=1    → 原文（text/plain，不加任何壳）
    #   🔴 存在理由（东哥 2026-09-28）：沪市上交所下载口的响应头是
    #        Content-Type: application/octet-stream
    #        Content-Disposition: attachment; filename="ssepcf_{code}_{date}.xml"
    #      ⇒ 浏览器只会**另存文件**、不在新窗口渲染（深市 CDN 是 text/plain ⇒ 直链可读）。
    #      改走本机代理后，若原样吐 XML（513500 = 205KB / 503 个 <Component> 标签堆）
    #      **可读性极差** ⇒ 交给 core/pcf_render.py 渲染成 HTML。
    #   ⚠️ 只服务沪市 ETF（51/52/56/58xxxx）：深市 CDN 直链本身已可读，不需要绕本机。
    #   ⚠️ 渲染层**只做呈现**，数字与 XML 原文逐字一致（不落库、不参与估值）。
    def _send_pcf_view(self):
        from urllib.parse import urlparse, parse_qs
        import re as _re
        q = parse_qs(urlparse(self.path).query)
        code = (q.get("code", [""])[0] or "").strip().upper().split(".")[0]
        raw_flag = (q.get("raw", ["0"])[0] or "0") == "1"
        if not _re.fullmatch(r"[0-9A-Z]{5,8}", code):
            self._json(400, {"error": "code 非法"})
            return
        try:
            import iopv_pcf as P
            if not P.is_sse_etf(code):
                self._json(400, {"error": "本页只服务沪市 ETF（51/52/56/58xxxx）；深市请直接用 CDN 直链"})
                return
            import pcf_render as R
            raw = R.fetch_sse_xml(code)
            if raw_flag:
                body = R.decode_xml(raw).encode("utf-8")
                ctype = "text/plain; charset=utf-8"
            else:
                # 沪市 XML 里没有基金名称 ⇒ 从 fund_profile 取，仅用于标题显示
                nm = ""
                try:
                    _c = get_conn()
                    _r = _c.execute("SELECT fund_name FROM fund_profile WHERE fund_code=?",
                                    (code,)).fetchone()
                    _c.close()
                    nm = _r[0] if _r and _r[0] else ""
                except Exception:
                    pass
                body = R.render_sse_html(code, raw, name_hint=nm).encode("utf-8")
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=300")
            self.end_headers()
            self.wfile.write(body)
        except ValueError as e:                    # 上游未发布 / 非沪市代码模板
            _logf("pcf/view 上游无有效 PCF: %s (%s)" % (code, e))
            self._json(404, {"error": "交易所未返回有效 PCF（可能尚未发布）", "detail": str(e)})
        except Exception as e:
            _logf("pcf/view 失败 %s: %s" % (code, e))
            self._json(502, {"error": "抓取上游 PCF 失败: %s" % e})

    def _send_pcf(self):
        try:
            code = self.path.strip('/').split('/')[2] if len(self.path.strip('/').split('/')) >= 3 else None
            if not code:
                self._json(400, {"error": "缺 code"})
                return
            SEL = ("SELECT trade_date, units, unit_nav, nav, cash_diff, est_cash_part, source, fetched_at "
                   "FROM pcf_snapshot WHERE fund_code=? AND trade_date=?")
            c = get_conn()
            latest = c.execute(
                "SELECT trade_date FROM pcf_snapshot WHERE fund_code=? "
                "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
            if not latest:
                c.close()
                self._json(404, {"error": "无 PCF 数据（请先抓取或粘贴）"})
                return
            # 优先展示最新公告；若它没有成分（只写了快照/尚未抓全），
            # 回退到「最近一份有成分的公告」（2026-09-15 修 Bug B：原来直接报「无成分」）
            comp_td, fallback = latest[0], False
            n_latest = c.execute("SELECT COUNT(*) FROM pcf_component WHERE fund_code=? AND trade_date=?",
                                 (code, latest[0])).fetchone()[0]
            if n_latest == 0:
                alt = c.execute("SELECT MAX(trade_date) FROM pcf_component WHERE fund_code=?",
                                (code,)).fetchone()
                if alt and alt[0] and alt[0] != latest[0]:
                    snap_alt = c.execute(SEL, (code, alt[0])).fetchone()
                    if snap_alt:
                        comp_td, fallback = alt[0], True
            snap = c.execute(SEL, (code, comp_td)).fetchone()
            rows = c.execute(
                "SELECT stock_code, name, shares, cash_sub_flag, premium_ratio, "
                "discount_ratio, subst_amount, market, cash_value FROM pcf_component "
                "WHERE fund_code=? AND trade_date=?", (code, comp_td)).fetchall()
            comps = [{"stock_code": r[0], "name": r[1], "shares": r[2], "cash_sub_flag": r[3],
                      "premium_ratio": r[4], "discount_ratio": r[5],
                      "subst_amount": r[6], "market": r[7], "cash_value": r[8]} for r in rows]
            c.close()
            self._json(200, {
                "fund_code": code,
                "snapshot": {"trade_date": snap[0], "units": snap[1], "unit_nav": snap[2],
                             "nav": snap[3], "cash_diff": snap[4], "est_cash_part": snap[5],
                             "source": snap[6], "fetched_at": snap[7]},
                "components": comps,
                "components_trade_date": comp_td,
                "latest_trade_date": latest[0],
                "fallback": fallback})
        except Exception as e:
            self._json(500, {"error": str(e)})

    # ---- FX 只读查询 ----
    def _send_fx(self):
        try:
            code = self.path.strip('/').split('/')[2] if len(self.path.strip('/').split('/')) >= 3 else None
            if not code:
                self._json(400, {"error": "缺 code"})
                return
            self._json(200, self._iopv_fx(code))
        except Exception as e:
            self._json(500, {"error": str(e), "type": "fx_error"})

    def _send_snapshot(self):
        try:
            data = generate_snapshot()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            _logf("快照生成失败: " + str(e))
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _send_history(self):
        """获取基金历史数据（用于历史记录弹窗）；逐日静态估值由引擎统一计算。"""
        code = self.path.split('/')[-1]
        try:
            db_code = code.split('.')[0]
            if db_code in sv.ETF_BENCHMARKS:
                result = sv.history_series(db_code)
            else:
                conn = get_conn()
                rows = conn.execute(
                    "SELECT date, nav, price FROM unified_fund_history WHERE fund_code=? ORDER BY date DESC LIMIT 30",
                    (db_code,),
                ).fetchall()
                conn.close()
                result = []
                for date, nav, price in rows:
                    premium = round((price / nav - 1) * 100, 2) if (price and nav) else None
                    result.append({
                        'date': date, 'nav': nav, 'price': price,
                        'idx': None, 'static_val': None, 'premium': premium, 'val_error': None,
                    })

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        except Exception as e:
            _logf("历史数据获取失败: " + str(e))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps([], ensure_ascii=False).encode())

    def _send_gpcsp(self):
        price, date = _get_gpcsp_price()
        if price:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'price': price, 'date': date}).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'price': None, 'date': None}).encode())


# ============ 定时任务 ============

def _snapshot_loop():
    """后台生成快照"""
    while True:
        try:
            data = generate_snapshot()
            with open(SNAPSHOT, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            _logf("快照生成失败: " + str(e))
        time.sleep(3)


def _ensure_today_pcf():
    """每天首次运行时确保「今日」PCF 已**完整**入库（快照 + 成分）。

    库里已有今日且成分齐全 → 跳过（每天只爬一次）。
    注意用 check_today_complete 而非 check_today_exists：只写快照没写成分同样视为缺失，
    否则 PCF 明细弹窗空窗、且昨日的篮子永远无法校准。

    🔴 2026-09-16 两处改动：
      1) 去掉 `pcf_source != 'sse_xml'` 的硬过滤 —— 改由 `fetch_and_store` 按配置分派
         （`sse_xml` 上交所 / `szse_txt` 深交所），未接入的源只记日志跳过。
      2) 抓到新公告后**立刻补校准**：新公告是"前一日篮子可校准"的唯一前提条件，
         而原来只靠 `last_calib_date` 一天跑一次 —— 若当天首次运行发生在 PCF 到位之前
         （如 8:00 抓不到、10:00 才抓到），这一天就再也不会校准了。"""
    if not _IOPV_OK:
        return
    try:
        today = datetime.now().strftime("%Y%m%d")
    except Exception:
        return
    try:
        c = get_conn()
        rows = c.execute(
            "SELECT fund_code, pcf_source FROM fund_iopv_config WHERE enabled=1").fetchall()
        c.close()
    except Exception as e:
        _logf("PCF 每日检查：读取配置失败 " + str(e))
        return
    for code, src in rows:
        src = (src or "").lower()
        if src not in ("sse_xml", "szse_txt"):
            _logf("PCF 每日检查 %s：pcf_source=%r 未接入取数通道，跳过" % (code, src))
            continue
        try:
            f = PCFFetcher()
            if f.check_today_complete(code):
                _logf("PCF 每日检查 %s：今日(%s) 快照+成分已在库，跳过抓取" % (code, today))
                continue
            sid, cnt = f.fetch_and_store(code)
            _logf("PCF 每日检查 %s：已抓取今日(%s) PCF，snapshot_id=%s，成分 %d 条"
                  % (code, today, sid, cnt))
            if cnt == 0:
                _logf("PCF 每日检查 %s：⚠️ 成分解析为 0，请核查公告结构" % code)
            # 新公告一到 → 前一日篮子此刻才具备「成分 + 收盘 + 中间价」→ 立刻补校准
            _calibrate_pending_pcf()
            _log_state["last_calib_date"] = datetime.now().strftime("%Y%m%d")
        except Exception as e:
            _logf("PCF 每日检查 %s 失败：%s" % (code, str(e)))


def _calibrate_pending_pcf():
    """补齐所有「条件已具备但还没算」的篮子（幂等）。

    两件事（都绝不在条件不全时兜底造数）：
      A. 篮子校准（表 iopv_verify，键 = 篮子基准日 D）：
         对象 = pcf_snapshot 的每个 pre_trade_date；需要 D 的成分(= 下一份公告的篮子)
         + D 日港股收盘 + D 日中间价。新公告一到，前一日篮子即可校准。
      B. 收盘 IOPV（表 iopv_close，键 = 公告日 T，2026-09-15 新增）：
         对象 = 每个「有成分」的公告日 T；需要 T 日港股收盘 + T 日中间价 + T 公告预估现金。
         这是历史页行 T 的「收盘IOPV」列，T 当天盘后即可算（与主看板实时 IOPV 同口径）。"""
    if not _IOPV_OK:
        return
    try:
        c = get_conn()
        codes = [r[0] for r in c.execute("SELECT fund_code FROM fund_iopv_config WHERE enabled=1")]
        c.close()
    except Exception as e:
        _logf("PCF 校准：读取配置失败 " + str(e))
        return
    for code in codes:
        try:
            pending = IOPVVerifier(code).verify_all_pending()
        except Exception as e:
            _logf("PCF 校准 %s 异常：%s" % (code, str(e)))
            pending = []
        for r in pending:
            if r.get("skipped"):
                _logf("PCF 校准 %s 篮子=%s 跳过：%s" % (code, r["basket_td"], r["skipped"]))
            else:
                _logf("PCF 校准 %s 篮子=%s(公告%s) 现算=%.2f 官方=%.2f 误差=%+.4fbp [%s]%s"
                      % (code, r["basket_td"], r["announce_td"], r["computed_basket"],
                         r["official_basket"], r["error_bps"], r["status"],
                         (" " + r["note"]) if r.get("note") else ""))
        # B. 收盘 IOPV（按公告日 T）
        try:
            for r in IOPVCloseCalc(code).calc_all():
                if r.get("skipped"):
                    _logf("收盘IOPV %s 公告=%s 跳过：%s" % (code, r["announce_td"], r["skipped"]))
                else:
                    _logf("收盘IOPV %s 公告=%s 篮子=%.2f + 预估现金=%.2f → IOPV=%.6f [%s]"
                          % (code, r["announce_td"], r["basket"], r["est_cash_part"],
                             r["iopv"], r["status"]))
        except Exception as e:
            _logf("收盘IOPV %s 异常：%s" % (code, str(e)))


def _daily_collect_loop():
    """每日定时采集 GPCSP006 + 每天首次确保当日 PCF 已入库 + 补齐篮子校准"""
    while True:
        try:
            now = datetime.now()
            # 每天 8:00 后确保当日 PCF 已入库（东哥：每天第一次运行先检查，是今天则跳过）
            if now.hour >= 8:
                # 结算汇兑比率：每天首次自动抓「最新一期」（源本来就只给 T-1，
                # 与主看板「实际折溢价(T-1结算)」口径天然吻合）—— 手动喂表从此退休。
                if now.strftime("%Y%m%d") != _log_state.get("last_settle_date"):
                    _log_state["last_settle_date"] = now.strftime("%Y%m%d")
                    try:
                        got = FXProvider("513590").fetch_settle_latest()
                        _logf("结算汇率自动抓取: " + (", ".join(
                            "%s %s 买%s/卖%s" % g for g in got) if got else "无返回（不兜底）"))
                    except Exception as e:
                        _logf("结算汇率自动抓取失败: " + str(e))
                _ensure_today_pcf()
                # LOF 静态估值数据：净值全历史（腾讯 year_all）+ 指数日K（东财 kline）
                #   —— 过期即补（core/static_val 内 12h TTL，源只给全量 ⇒ 抓取幂等）
                if now.strftime("%Y%m%d") != _log_state.get("last_staticval_date"):
                    _log_state["last_staticval_date"] = now.strftime("%Y%m%d")
                    try:
                        import static_val as _sv
                        _conn2 = get_conn()
                        # 2026-10-08 东哥拍板：E/A ETF 行也出「T-1 静态估值」两列（与
                        # LOF/黄金原油同构）⇒ 非 woody 且有 idx_code 的 E/A 基金一并
                        # 过期即补。放在本后台线程做（~170 只首轮约 1 分钟），不阻塞看板；
                        # fetch_nav_history 内部按 in_arbtest 分区写红线二次兜底。
                        _rows = _conn2.execute(
                            "SELECT fund_code, idx_code FROM fund_profile "
                            "WHERE fund_type='LOF' OR jsl_market='C' "
                            "OR (jsl_market IN ('E','A') AND (in_arbtest IS NULL OR in_arbtest!=1) "
                            "AND idx_code IS NOT NULL AND idx_code!='')").fetchall()
                        _conn2.close()
                        _sv.ensure_nav_history([r[0] for r in _rows], DB_PATH)
                        _sv.ensure_index_history([r[1] for r in _rows if r[1]], DB_PATH)
                        # 沙特双雄：02830 港股日K 过期即补（估值驱动 = 02830 × HKD/CNY）
                        try:
                            import saudi_bridge as _sb
                            _sb.ensure_hk_daily(DB_PATH)
                        except Exception as e:
                            _logf("沙特 02830 日K 刷新失败: " + str(e))
                        _logf("静态估值数据已刷新: %d 只（LOF/C + E/A 非woody ETF）" % len(_rows))
                    except Exception as e:
                        _logf("静态估值数据刷新失败: " + str(e))
                # 每日首次（含启动首轮）补跑一次篮子校准：新公告 → 前一日篮子可校准
                if now.strftime("%Y%m%d") != _log_state.get("last_calib_date"):
                    _log_state["last_calib_date"] = now.strftime("%Y%m%d")
                    _calibrate_pending_pcf()
            # 每天 16:00 后采集一次（所有配置了 secid 的基准指数）
            if now.hour >= 16 and (time.time() - _log_state.get("last_collect_time", 0)) > 86400:
                _log_state["last_collect_time"] = time.time()
                for code, cfg in sv.ETF_BENCHMARKS.items():
                    secid = cfg.get("secid")
                    if not secid:
                        continue
                    sym = cfg["index"]
                    price, date = _get_index_price(secid, sym, cfg.get("idx_scale", 100))
                    if price:
                        _logf("每日采集 %s(%s) 成功: %s (%s)" % (code, sym, price, date))
                    else:
                        _logf("每日采集 %s(%s) 失败，使用缓存数据" % (code, sym))
        except Exception as e:
            _logf("每日采集异常: " + str(e))
        # 每小时检查一次
        time.sleep(3600)


def _share_collect_loop():
    """场内份额日更：每日首次从**交易所直采**（深沪 LOF/ETF），落 unified_fund_history。

    2026-09-28 架构变更（东哥拍板）：ETFarb **不再读东京 VPS 份额**，改交易所直采。
      · 深 LOF 16xxxx / 深 ETF 158·159xxx → 深交所官方 scsj_fund_jjgm（带日期窗可补历史）
      · 沪 LOF 501·502·506              → 上交所官方 LOFGMTJ（逐日全量）
      · 沪 ETF 51·52·56·58              → 腾讯 qt.gtimg.cn 派生（交易所不公布，仅当天）
    单库方案（2026-10-05）：落共享 arb_master.db 的 unified_fund_history（与 arbTest 同文件）。
    独立线程 ⇒ 不阻塞 PCF / 静态估值 / 看板预热等其他启动任务。
    """
    time.sleep(20)                      # 让服务与预热线程先起来
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if now.hour >= 8 and today != _log_state.get("last_share_date"):
                try:
                    _day = now.strftime("%Y-%m-%d")
                    # 🔴 按**市场覆盖**判断，而非全 source 行数：
                    #    只看总行数会因「沪 ETF 腾讯派生先落地」而误判深市已采（2026-09-28 踩坑）。
                    _c = get_conn()
                    n_sz = _c.execute(
                        "SELECT COUNT(*) FROM unified_fund_history WHERE source='szse_jjgm' AND fetched_at LIKE ?",
                        (_day + "%",)).fetchone()[0]
                    n_sh = _c.execute(
                        "SELECT COUNT(*) FROM unified_fund_history WHERE source IN ('sse_lofgm','tencent_derived') AND fetched_at LIKE ?",
                        (_day + "%",)).fetchone()[0]
                    _c.close()
                    if n_sz >= 100 and n_sh >= 100:
                        _log_state["last_share_date"] = today
                        _logf("份额直采: 今日已采（深 %d / 沪 %d），跳过" % (n_sz, n_sh))
                    else:
                        import share_direct_fetcher as _sdf
                        st = _sdf.ShareDirectFetcher().sync(days=30, verbose=False)
                        ok = ((st["sz_lof"] + st["sz_etf"]) >= 100
                              and (st["sh_lof"] + st["sh_etf"]) >= 100)
                        if ok:
                            _log_state["last_share_date"] = today
                        # 🔴 不完整时不置位 ⇒ 1 小时后再试（深交所限流通常是临时的，解封后自动补齐）
                        _logf("份额直采完成: 深LOF=%d 深ETF=%d 沪LOF=%d 沪ETF=%d 写行=%d 新增有值=%d 失败=%d%s"
                              % (st["sz_lof"], st["sz_etf"], st["sh_lof"], st["sh_etf"],
                                 st["rows"], st["new_filled"], len(st["failed"]),
                                 "" if ok else " [不完整，1 小时后重试]"))
                except Exception as e:
                    _logf("份额直采失败（1 小时后重试）: " + str(e))
        except Exception as e:
            _logf("份额采集循环异常: " + str(e))
        time.sleep(3600)


# ============ 主程序 ============

def main():
    # 创建目录
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    
    _logf("===== ETFarb 启动 =====")

    # 初始化 IOPV 告警表（P6）
    try:
        _init_iopv_alert_table()
        _logf("iopv_alert 表已就绪")
    except Exception as e:
        _logf("iopv_alert 表初始化跳过: " + str(e))

    # 启动后台线程
    t1 = threading.Thread(target=_snapshot_loop, daemon=True)
    t1.start()
    
    t2 = threading.Thread(target=_daily_collect_loop, daemon=True)
    t2.start()
    _logf("每日采集线程已启动（8:00 后确保当日 PCF 已入库；16:00 后采集 GPCSP006）")

    # 看板缓存预热（ARM 跨境网络慢，避免冷加载 47s 白屏）
    t3 = threading.Thread(target=Handler._prewarm_markets, daemon=True)
    t3.start()
    _logf("看板缓存预热线程已启动")

    # 场内份额日更（交易所直采，独立线程；2026-09-28 替代东京 VPS 份额同步）
    t4 = threading.Thread(target=_share_collect_loop, daemon=True)
    t4.start()
    _logf("场内份额直采线程已启动（每日首次从交易所直采深沪 LOF/ETF）")

    # 启动 HTTP 服务
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    bind_host = DEFAULT_BIND_HOST   # 默认 127.0.0.1（仅本机）；可用 ETFARB_BIND_HOST 覆盖

    print("=" * 50)
    print("ETFarb Dashboard")
    print("  Backend : http://%s:%d" % (bind_host, port))
    print("  Snapshot: %s" % SNAPSHOT)
    print("  Log     : %s" % SERVER_LOG)
    print("  Ctrl+C  : Stop")
    print("=" * 50)
    
    try:
        server = ThreadingHTTPServer((bind_host, port), Handler)
        server.serve_forever()
    except KeyboardInterrupt:
        _logf("ETFarb 已停止")
        print("\nETFarb 已停止")


if __name__ == "__main__":
    main()
