# -*- coding: utf-8 -*-
"""静态估值引擎（东哥 2026-09-18 拍板口径）。

公式（arbTest unified_valuation.basket_valuation 矩阵式、单组件、pos=0.95 特例）::

    静态估值(D) = nav(D-1) × (1 + 0.95 × 指数涨幅(D))       # 国内LOF 令 fx=1
    实时估值(T) = nav(D)   × (1 + 0.95 × 指数实时涨幅)       # 在 etf_iopv_quote LOF 分支

用途：静态估值是「基金公司公布净值之前」对 Nav(D) 的预估，与随后公布的
Nav(D) 核对答案、看误差 —— 验证估值算法的准确性（集思录同设计：点列弹历史页）。

数据管道（全部免费源、过期即补，东哥 2026-09-17 定「不做定时闹钟」）：
· 净值全历史：腾讯 `year_all/<code>.js`（gb2312，213ms/只）→ 宽表 `unified_fund_history(nav,acc)`
· 指数日K：  统一走腾讯 `web.ifzq.gtimg.cn` fqkline（港股 hkXXX + A股 sh/szXXX；
            东财 push2his 已死：2026-09-19 实测断连、加 Referer 也不行）→ 表 `index_history`
· 抓取节流： 表 `fetch_state`（12h TTL；源只给全量 ⇒ 抓取幂等，重抓无害）
"""
import datetime
import json
import re
import sqlite3
import ssl
import time
import urllib.request

from index_quote import clean_index_symbol, classify_index_symbol, _HK_PREFIXES
from core.db import DB_PATH, upsert_fund_history, get_conn  # 单库：arb_master.db
try:
    import saudi_bridge as _saudi           # 沙特双雄桥接（02830 × HKD/CNY）
except ImportError:                         # 以 core.saudi_bridge 形式导入时
    from core import saudi_bridge as _saudi

_HERE = None  # 本模块由 core/ 下其它模块以同目录 import 使用

PROJ_DB = None  # 延迟取：由调用方传，或用 etf_iopv_quote.DB_PATH


def _is_in_arbtest(code):
    """分区写红线：in_arbtest=1 的 48 只 nav/acc 归 arbTest，ETFarb 跳过不碰。"""
    try:
        c = get_conn()
        try:
            r = c.execute("SELECT in_arbtest FROM fund_profile WHERE fund_code=?",
                          (str(code),)).fetchone()
            return bool(r and r[0])
        finally:
            c.close()
    except Exception:
        return False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

POS = 0.95                     # 东哥 2026-09-18：仓位统一 95%
_TTL_SEC = 12 * 3600           # fetch_state 节流：12h 过期即补

_YEAR_ALL_URL = ("https://stockjs.finance.qq.com/fundUnitNavAll/data/"
                 "year_all/%s.js")
_TENCENT_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                      "param=%s,day,,,400,qfq")
_DATA_BEG = "2026-01-01"       # 东哥 2026-09-19：历史数据只保 2026 年起


def _db(db=None):
    return sqlite3.connect(db or _default_db())


def _default_db():
    # 单库方案：统一落 arb_master.db（与 arbTest 同文件）
    return DB_PATH


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode(
        "gbk", "ignore")


def _ensure_tables(c):
    # 净值历史已并入统一宽表 unified_fund_history（见 _migrate_fragments.py），
    # 此处不再建 fund_nav_history；只确保 ETFarb 自有表存在。
    c.execute("""CREATE TABLE IF NOT EXISTS fetch_state (
        src TEXT NOT NULL, key TEXT NOT NULL, last_ok_ts TEXT,
        PRIMARY KEY (src, key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS index_history (
        symbol TEXT, date TEXT, close REAL, source TEXT DEFAULT 'eastmoney',
        PRIMARY KEY (symbol, date))""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_ih_sym_date
        ON index_history (symbol, date)""")


def _stale(c, src, key, now_ts):
    row = c.execute("SELECT last_ok_ts FROM fetch_state WHERE src=? AND key=?",
                    (src, key)).fetchone()
    if not row or not row[0]:
        return True
    try:
        return (now_ts - datetime.datetime.fromisoformat(row[0])).total_seconds() > _TTL_SEC
    except Exception:
        return True


def _mark_ok(c, src, key, now_ts):
    c.execute("""INSERT INTO fetch_state (src, key, last_ok_ts) VALUES (?, ?, ?)
                 ON CONFLICT(src, key) DO UPDATE SET last_ok_ts=excluded.last_ok_ts""",
              (src, key, now_ts.isoformat()))


# ---------------------------------------------------------------------------
# 净值全历史（腾讯 year_all）
# ---------------------------------------------------------------------------
def _fetch_nav_qq(code, beg="2026-01-01"):
    """腾讯 year_all 净值历史 → [(code,iso,nav,acc,source)]。"""
    try:
        raw = _get(_YEAR_ALL_URL % code)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return []
        data = (json.loads(m.group(0)).get("data") or [])
    except Exception:
        return []
    rows = []
    for it in data:
        try:
            d = str(it[0])
            iso = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
            if iso < beg:
                continue
            nav = float(it[1])
            acc = float(it[2]) if len(it) > 2 and it[2] else None
            rows.append((code, iso, nav, acc, "qq_year_all"))
        except (ValueError, IndexError, TypeError):
            continue
    return rows


def _fetch_nav_em(code, beg="2026-01-01", page_size=400):
    """天天基金 lsjz 兜底源（部分货基 腾讯 year_all 为空，如 511990）。"""
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
               "&pageIndex=1&pageSize=%d" % (code, page_size))
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("{"); e = raw.rfind("}") + 1
        if s < 0 or e <= s:
            return []
        data = json.loads(raw[s:e])
        recs = data.get("Data", {}).get("LSJZList", [])
        out = []
        for r in recs:
            d = r.get("FSRQ"); nav = r.get("DWJZ")
            if not (d and nav):
                continue
            try:
                iso = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) >= 8 else str(d)
                if iso < beg:
                    continue
                nv = float(nav)
                if nv < 50:   # 防御：511990 等 eastmoney 尺度异常（0.275），不入历史
                    continue
                out.append((code, iso, nv, None, "em_lsjz"))
            except (ValueError, TypeError):
                continue
        return out
    except Exception:
        return []


def _latest_nav_qq_jj(code):
    """腾讯 jj 实时净值（无历史源时的「当日累计」兜底，如 511990）。

    511990 在腾讯 year_all 空、东财 lsjz 尺度异常（0.275），无可靠历史源。
    东哥 2026-09-30 口径：不补历史、从现在起累计 —— 用腾讯 jj 实时净值落当日行，
    由每日刷新线程（_daily_collect_loop）逐日累积。返回 nav 或 None。"""
    try:
        sym = ("sh" if code.startswith(("5", "6")) else "sz") + code
        raw = _get("https://qt.gtimg.cn/q=%s" % sym)
        m = re.search(r'="([^"]*)"', raw)
        if not m:
            return None
        f = m.group(1).split("~")
        if len(f) < 4 or not f[3]:
            return None
        return float(f[3])
    except Exception:
        return None


def fetch_nav_history(code, db=None, beg="2026-01-01"):
    """拉一只基金净值入 `fund_nav_history`（腾讯 year_all 优先；空则回退天天基金 lsjz）。
    只保留 beg 起的记录。返回写入行数（0=双源无数据）。

    🔴 511990 特例：双源都空/异常时，用腾讯 jj 实时净值落「当日」行（累计起点），
       后续每日刷新线程会逐日补齐，不做历史回补。"""
    rows = _fetch_nav_qq(code, beg) or _fetch_nav_em(code, beg)
    if not rows:
        jj = _latest_nav_qq_jj(code)
        if jj:
            today = datetime.date.today().strftime("%Y-%m-%d")
            rows = [(code, today, jj, None, "qq_jj_realtime")]
        else:
            return 0
    # 分区写红线：arbTest 的 48 只净值归 arbTest，ETFarb 跳过的净值写，避免覆盖。
    if _is_in_arbtest(code):
        return 0
    n = 0
    for code_, iso, nav, acc, source in rows:
        # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 nav/acc 是谁写的"（凌晨批来源排查教训）
        upsert_fund_history(code_, iso, {"nav": nav, "acc": acc, "source": source},
                            writer="etfarb:static_val")
        n += 1
    c = _db(db)
    try:
        _ensure_tables(c)
        _mark_ok(c, "nav_hist", code, datetime.datetime.now())
        c.commit()
    finally:
        c.close()
    return n


def ensure_nav_history(codes, db=None, beg="2026-01-01"):
    """过期即补：12h 内抓过的跳过。返回 {code: n_rows}（仅本轮抓的）。"""
    now_ts = datetime.datetime.now()
    c = _db(db)
    try:
        _ensure_tables(c)
        todo = [k for k in codes if _stale(c, "nav_hist", k, now_ts)]
    finally:
        c.close()
    out = {}
    for k in todo:
        out[k] = fetch_nav_history(k, db, beg=beg)
        time.sleep(0.05)
    return out


# ---------------------------------------------------------------------------
# 指数日K（统一腾讯 fqkline；东财 push2his 已死：2026-09-19 实测断连）
# ---------------------------------------------------------------------------
def _tencent_index_symbol(cs, kind):
    """clean 后的指数 → 腾讯 fqkline 代码。A股 399/159→sz、其余→sh；港股 hk+。"""
    if kind == "hk":
        return "hk" + cs
    if kind == "ashare":
        return ("sz" if cs.startswith(("399", "159")) else "sh") + cs
    return None  # 93xxxx 未映射 CSI 码腾讯无免费源 ⇒ 0


def _hist_symbol(idx_code):
    """历史通道 symbol：东财 secid（`0.399997` `1.000922` `2.930713` `124.HSTECH`）
    一律剥掉数字市场前缀成裸码 —— 历史只认 腾讯 fqkline / 新浪日K 的 sh/sz/hk 裸码表。
    （与实时通道不同：实时对 2./100./124. 需保留 secid 直连东财 push2delay。）"""
    s = clean_index_symbol(idx_code)
    if s and "." in s:
        mkt, rest = s.split(".", 1)
        if mkt.isdigit() and rest:
            return rest
    return s


def _fetch_index_history_tencent(tsym, cs, db=None):
    """指数日K收盘：腾讯 fqkline（港股 hkXXX + A股 sh/szXXX 统一走此口）。

    源缺口（如 HSCI 腾讯仅到 2011-01-21）⇒ 0（上层按「无免费历史源」处理，
    静态估值留空，与 ETF 的 no_source 同构）。只保留 _DATA_BEG 起的记录。
    🔴 0 行也算「已探测」（2026-10-08）：源确实没这个指数（如 hkHSHKBIO），
    记 fetch_state 走 12h TTL，别每次 build 都重复打失败源；异常（网络抖动）
    不记，下轮重试。"""
    ok = False
    try:
        j = json.loads(_get(_TENCENT_KLINE_URL % tsym))
        node = ((j.get("data") or {}).get(tsym) or {})
        lines = node.get("qfqday") or node.get("day") or []
        ok = True
    except Exception:
        return 0
    rows = []
    for it in lines:
        if len(it) < 3:
            continue
        try:
            d = str(it[0])
            if d < _DATA_BEG:
                continue
            rows.append((cs, d, float(it[2]), "tencent_kline"))
        except (ValueError, TypeError):
            continue
    if not rows and not ok:
        return 0
    c = _db(db)
    try:
        _ensure_tables(c)
        if rows:
            c.executemany("""INSERT INTO index_history (symbol, date, close, source)
                             VALUES (?, ?, ?, ?)
                             ON CONFLICT DO NOTHING""", rows)
        _mark_ok(c, "idx_hist", cs, datetime.datetime.now())
        c.commit()
        return len(rows)
    finally:
        c.close()


_CSINDEX_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
                "?indexCode=%s&startDate=%s&endDate=%s")


def _fetch_index_history_csindex(cs, db=None):
    """中证官网 index-perf 日收盘（2026-10-08 接入：93xxxx 中证港股通系指数
    腾讯无免费日K（实测 sh93xxxx 全空），中证官网口实测可用；Referer 必须。
    tradeDate=YYYYMMDD → index_history.date=YYYY-MM-DD；只保留 _DATA_BEG 起。
    国证 98xxxx 中证官网无数据 ⇒ 0 行记已探测，显「—」宁空勿错。"""
    try:
        url = _CSINDEX_URL % (cs, _DATA_BEG.replace("-", ""),
                              datetime.date.today().strftime("%Y%m%d"))
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Referer": "https://www.csindex.com.cn/"})
        j = json.loads(urllib.request.urlopen(
            req, timeout=15, context=_CTX).read().decode("utf-8", "ignore"))
        data = j.get("data") or []
    except Exception:
        return 0
    rows = []
    for it in data:
        d = str(it.get("tradeDate") or "")
        close = it.get("close")
        if len(d) != 8 or close in (None, ""):
            continue
        iso = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
        if iso < _DATA_BEG:
            continue
        try:
            rows.append((cs, iso, float(close), "csindex"))
        except (TypeError, ValueError):
            continue
    c = _db(db)
    try:
        _ensure_tables(c)
        if rows:
            c.executemany("""INSERT INTO index_history (symbol, date, close, source)
                             VALUES (?, ?, ?, ?)
                             ON CONFLICT DO NOTHING""", rows)
        _mark_ok(c, "idx_hist", cs, datetime.datetime.now())
        c.commit()
        return len(rows)
    finally:
        c.close()


def _fetch_index_history_eastmoney(secid, cs, db=None):
    """东财 push2his 日K 收盘（富时/自定义 secid 口径：249.GPCSP006 / 2.930713 等）。

    大山的 GPCSP006 实测走东财（secid=249.GPCSP006）；本机 IP 常被东财反爬拦截
    （Remote end closed），但 ARM 云 IP 可正常拉取。需伪装 Referer+UA（同源
    core/backfill_gpcsp_history.py 验证过的头）。f53=真实点位，原样落库。
    返回行数；0 行记已探测（12h TTL）。真实值压过 manual/provisional：用 REPLACE。"""
    try:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
               "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0"
               "&end=20500101&lmt=400" % secid)
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Referer": "https://quote.eastmoney.com/",
        })
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = op.open(req, timeout=15).read().decode("utf-8", "ignore")
        j = json.loads(raw)
        klines = ((j.get("data") or {}).get("klines") or [])
    except Exception:
        return 0
    rows = []
    for kl in klines:
        p = kl.split(",")
        if len(p) < 3:
            continue
        d = p[0]                      # 东财 kline 日期已是 YYYY-MM-DD
        if len(d) != 10 or d < _DATA_BEG:
            continue
        try:
            rows.append((cs, d, float(p[2]), "eastmoney"))
        except (ValueError, TypeError):
            continue
    c = _db(db)
    try:
        _ensure_tables(c)
        if rows:
            c.executemany("""INSERT OR REPLACE INTO index_history
                             (symbol, date, close, source) VALUES (?, ?, ?, ?)""", rows)
        _mark_ok(c, "idx_hist", cs, datetime.datetime.now())
        c.commit()
        return len(rows)
    finally:
        c.close()


def fetch_index_history(idx_code, db=None, beg="20240101"):
    """拉一只指数的日K收盘入 `index_history`（symbol=clean 后的码）。返回行数。

    🔴 统一走腾讯 fqkline（港股 hkXXX + A股 sh/szXXX）；东财 push2his 已死
    （2026-09-19 实测断连，加 Referer 也不行）。
    2026-10-08 扩两条道（E/A ETF 静态估值，东哥拍板）：
    · 中证 9xxxxx（93xxxx 港股通主题等）→ 中证官网 index-perf（腾讯实测全空）；
    · em_secid 恒生系名码（HSTECH/HSCEI/HSI…）→ 腾讯 hk+名 尽力一试
      （实测 HSTECH/HSCEI/HSI/CES100/H11153 有数据，其余恒生主题无 ⇒ 0）。
    beg 参数保留兼容旧调用方，实际截断日期由 _DATA_BEG 统一控制。"""
    cs = _hist_symbol(idx_code)
    if not cs:
        return 0
    kind, _ = classify_index_symbol(idx_code)
    if kind == "hk":
        tsym = "hk" + cs
    elif cs.isdigit() and len(cs) == 6:
        if cs.startswith("9"):
            return _fetch_index_history_csindex(cs, db)   # 中证/国证 9xxxxx
        tsym = ("sz" if cs.startswith(("399", "159")) else "sh") + cs
    elif kind == "ashare":
        tsym = ("sz" if cs.startswith(("399", "159")) else "sh") + cs
    elif kind == "em_secid" and not cs.isdigit():
        # 富时/自定义 secid（249.GPCSP006 / 2.930713 等）→ 东财 push2his（大山口径，需伪装 header）
        if re.match(r"^\d+\.[A-Za-z0-9]+$", idx_code) and cs not in _HK_PREFIXES:
            n = _fetch_index_history_eastmoney(idx_code, cs, db)
            if n:
                return n
        # 恒生系名码（HSI/HSTECH…）→ 腾讯 hk+ 尽力一试（兜底）
        return _fetch_index_history_tencent("hk" + cs, cs, db)
    else:
        return 0  # 93xxxx 未映射 CSI 码腾讯无免费源 ⇒ 0
    return _fetch_index_history_tencent(tsym, cs, db)


def ensure_index_history(idx_codes, db=None):
    """过期即补（按 clean 后的 symbol 去重）。返回 {sym: n_rows}。"""
    now_ts = datetime.datetime.now()
    syms = {}
    for k in idx_codes:
        cs = _hist_symbol(k)
        if cs and cs not in syms:
            syms[cs] = k
    c = _db(db)
    try:
        _ensure_tables(c)
        todo = [cs for cs in syms if _stale(c, "idx_hist", cs, now_ts)]
    finally:
        c.close()
    out = {}
    for cs in todo:
        out[cs] = fetch_index_history(syms[cs], db)
        time.sleep(0.05)
    return out


# ---------------------------------------------------------------------------
# 静态估值计算
# ---------------------------------------------------------------------------
def _idx_close_series(c, idx_code):
    """该基金跟踪指数的 {date: close}（历史通道裸码口径，secid 已剥前缀）。"""
    cs = _hist_symbol(idx_code)
    if not cs:
        return {}
    return {d: v for d, v in c.execute(
        "SELECT date, close FROM index_history WHERE symbol=? ORDER BY date", (cs,))}


def _close_on_or_before(series, date):
    """series 里 ≤ date 的最近一个收盘。"""
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return (best, series[best]) if best else (None, None)


def _lcu_bridge_series(c, code):
    """159687 等 LCU 驱动基金：返回 LCU 官方 NAV 序列 {date: nav}；非 LCU 返回 None。

    159687（亚太精选 CSOP LCU）几乎纯持 CSOP LCU ETF，其底层净值是基金净值的
    直接驱动。用 LCU NAV 序列做桥接，σ 从 ~83bp 降到 ~4bp，与主看板 T-1 列同口径。
    白名单仅 159687（避免误伤其他 GPCSP006 指数基金）。"""
    if code not in ('159687',):
        return None
    try:
        rows = c.execute(
            "SELECT date, nav FROM lcu_nav_history ORDER BY date").fetchall()
    except Exception:
        return None
    if len(rows) < 2:
        return None
    return {d: v for d, v in rows}


def static_history(code, days=20, db=None, fund_type=None, idx_code=None,
                   with_nav_history=True):
    """静态估值历史：[{date, nav, static_val, err_pct}, ...]（date 降序，≤days 条）。

    static_val(D) = nav(D-1) × (1 + POS × (指数D收盘 ÷ 指数D-1收盘 − 1))
    err_pct      = (nav(D) ÷ static_val(D) − 1) × 100     # 「核对答案」的误差
    数据不足（nav 或指数历史缺）的日期不出行 —— 宁空勿错。

    159687（LCU 驱动）走 LCU 桥接：static_val(D) = nav(D-1) × LCU[D]/LCU[D-1]，
    与主看板 T-1 列口径一致，误差≈0（不再用 GPCSP006 指数法）。

    520830/159329（沙特双雄，100% 持港股 02830）走「02830 × HKD/CNY 桥接」：
        static_val(D) = nav(D-1) × [02830(D)/02830(D-1)] × [fx(D)/fx(D-1)]"""
    if code in _saudi.FUNDS:
        return _saudi.static_history(code, days=days, db=db)
    c = _db(db)
    try:
        _ensure_tables(c)
        navs = [(d, v) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
        if len(navs) < 2:
            return []
        # LCU 桥接优先（159687）：用 LCU 官方 NAV 序列替代 GPCSP006 指数
        lcu_series = _lcu_bridge_series(c, code)
        if lcu_series:
            series = lcu_series
            use_lcu = True
        else:
            if idx_code is None:
                r = c.execute("SELECT idx_code FROM fund_profile WHERE fund_code=?",
                              (code,)).fetchone()
                idx_code = r[0] if r else None
            series = _idx_close_series(c, idx_code) if idx_code else {}
            use_lcu = False
        if len(series) < 2 and not use_lcu:
            return []
        nav_d = dict(navs)
        dates = [d for d, _ in navs]
        out = []
        for i in range(1, len(dates)):
            d, d0 = dates[i], dates[i - 1]
            b_date, b_px = _close_on_or_before(series, d0)
            c_date, c_px = _close_on_or_before(series, d)
            if not b_px or not c_px:
                continue
            if use_lcu:
                # LCU 全比例桥接（POS=1，LCU 已含汇率/FoF/时区噪声）
                static = navs[i - 1][1] * (c_px / b_px)
            else:
                static = navs[i - 1][1] * (1 + POS * (c_px / b_px - 1))
            if static <= 0:
                continue
            out.append({"date": d, "nav": navs[i][1], "static_val": round(static, 4),
                        "err_pct": (navs[i][1] / static - 1) * 100})
        return out[-days:]
    finally:
        c.close()


def latest_static(code, db=None):
    """看板行用：最新一日静态估值 + 误差。返回 dict 或 None。

    159687（亚太精选 QDII，双层 FoF）走「LCU 官方 NAV 桥接」增强：
        est = nav[T-1] × (LCU[T]/LCU[T-1] 优先，否则 GPCSP006[T]/GPCSP006[T-1] 兜底)
    相对纯指数法 σ 83bp → 4bp（降幅 95%，2026-10-08 回测）。引擎入口
    core.static_valuation.estimate_t1_lcu，本函数仅做字段对齐（不重复算法）。

    520830/159329（沙特双雄，100% 持港股 02830）走「02830 × HKD/CNY 桥接」：
        est = nav[anchor] × (02830[t1]/02830[anchor]) × (fx[t1]/fx[anchor])
    锚定最新公告净值向前投影一步到 T-1。实测复现官方口径（0.8609→0.8564）。
    引擎入口 core.saudi_bridge.estimate_t1，本函数仅做字段对齐。
    """
    if code in _saudi.FUNDS:
        try:
            r = _saudi.estimate_t1(code, db)
            if r and r.get("static_val") is not None:
                actual = r.get("actual")
                est = r["static_val"]
                return {
                    "date": r.get("as_of_date"),
                    "nav": actual,
                    "static_val": est,
                    "err_pct": (actual / est - 1) * 100 if actual else None,
                    "method": r.get("method"),
                }
        except Exception:
            pass  # 桥接失败降级回通用算法
    if code == "159687":
        try:
            from core.static_valuation import estimate_t1_lcu
            r = estimate_t1_lcu("159687")
            if r and r.get("static_val") is not None:
                actual = r.get("actual")
                est = r["static_val"]
                return {
                    "date": r.get("as_of_date"),
                    "nav": actual,
                    "static_val": est,
                    "err_pct": (actual / est - 1) * 100 if actual else None,
                    "method": r.get("method"),
                }
        except Exception:
            pass  # 桥接失败降级回通用算法
    rows = static_history(code, days=2, db=db)
    return rows[-1] if rows else None


def nav_change_pct(code, db=None):
    """「净值涨幅」列：最新已公布净值 ÷ 上一公布净值 − 1（%）。不足两天 None。"""
    c = _db(db)
    try:
        _ensure_tables(c)
        rows = c.execute(
            "SELECT nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date DESC LIMIT 2", (code,)).fetchall()
    finally:
        c.close()
    if len(rows) < 2 or not rows[1][0]:
        return None
    return (rows[0][0] / rows[1][0] - 1) * 100


def refresh_all(lof_codes, idx_codes, db=None):
    """采集循环入口：净值 + 指数历史一起过期即补。"""
    ensure_nav_history(lof_codes, db)
    ensure_index_history(idx_codes, db)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    db = DB_PATH
    code = sys.argv[1] if len(sys.argv) > 1 else "161119"
    n = fetch_nav_history(code, db)
    print("nav rows: %s" % n)
    import sqlite3
    c = sqlite3.connect(db)
    r = c.execute("SELECT idx_code FROM fund_profile WHERE fund_code=?", (code,)).fetchone()
    print("idx_code: %s" % (r and r[0]))
    if r and r[0]:
        print("idx rows: %s" % fetch_index_history(r[0], db))
    for row in static_history(code, days=10, db=db):
        print("%s nav=%.4f static=%.4f err=%+.2f%%" %
              (row["date"], row["nav"], row["static_val"], row["err_pct"]))
