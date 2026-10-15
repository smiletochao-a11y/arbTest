# -*- coding: utf-8 -*-
"""沙特双雄桥接估值（520830 沙特ETF华泰柏瑞 / 159329 沙特ETF南方）。

底层单一：两只都是「直接持有港股 02830.HK（南方东英沙特阿拉伯ETF，CSOP Saudi）」
的跨境 QDII-FoF，跟踪标的同为富时沙特阿拉伯指数。故估值驱动 = 02830 港股价 × 港币汇率：

    driver(t)    = 02830_close(t) × hkd_cny_mid(t)
    T-1 静态估值 = NAV(anchor) × driver(t1) / driver(anchor)
    实时估值(T)  = NAV(anchor) × driver(now) / driver(anchor)

  · anchor = 最新公告净值日（T-2）；t1 = anchor 之后第一个驱动日（T-1）；now = 最近日K/实时。
  · 同日对齐（实测最优：滞 0 日 mean|err| 0.59% vs 滞 1 日 0.70%，全库 179 样本回测）。
  · 复现官方口径：0.8609 × (77.50/77.82) × (0.85842/0.85935) = 0.8564 = 官方 T-1 估值。

旧口径（idx_code=100.FISAULMU 富时沙特指数）已弃用：东财 push2his 对云/家用 IP 均封禁
（RemoteDisconnected）⇒ index_history 里 FISAULMU 只有零星几行、且 09-29 缺 → 用 09-28
顶替 → 单日误差 -1.13%。改用 02830 港股日K（腾讯口稳定、400 天一次拉全）后误差降到 bp 级。

数据源（全免费）：
  · 02830 日K：腾讯 fqkline hk02830 → index_history(symbol='02830', source='tencent_kline')
  · 02830 实时：新浪 rt_hk02830（HKD，经 hk_quote 统一口）
  · 港币汇率：exchange_rate.hkd_cny_mid（中间价，与官方口径一致）
红线：本模块只读 + 只写 index_history 的 02830 行；不碰 fund_basket_weights / PCF。
"""
import datetime
import json
import sqlite3
import ssl
import urllib.request

from core.db import DB_PATH

HK_SYMBOL = "02830"                 # 港交所：南方东英沙特阿拉伯ETF
FUNDS = ("520830", "159329")        # 华泰柏瑞 / 南方 两只境内跨境 ETF
FX_COL = "hkd_cny_mid"              # 港币兑人民币中间价
_DATA_BEG = "2026-01-01"
_TTL_SEC = 12 * 3600

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_TENCENT_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                "param=hk%s,day,,,400,qfq")


def _db(db=None):
    return sqlite3.connect(db or DB_PATH)


def _ensure_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS index_history (
        symbol TEXT, date TEXT, close REAL, source TEXT DEFAULT 'eastmoney',
        PRIMARY KEY (symbol, date))""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_ih_sym_date
        ON index_history (symbol, date)""")
    c.execute("""CREATE TABLE IF NOT EXISTS fetch_state (
        src TEXT NOT NULL, key TEXT NOT NULL, last_ok_ts TEXT,
        PRIMARY KEY (src, key))""")


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
# 02830 港股日K（腾讯 fqkline）
# ---------------------------------------------------------------------------
def fetch_hk_daily(db=None):
    """拉 hk02830 日K收盘入 index_history(symbol='02830')。返回写入行数。

    腾讯 fqkline 一次给 400 天 ⇒ 首次即全量回补，之后每日增量（幂等）。
    🔴 不复权 `day` 字段：港股价 HKD 原值，f53 口径。source='tencent_kline'。
    只保留 _DATA_BEG 起；INSERT OR REPLACE（真实成交值压过任何旧的占位）。
    """
    try:
        req = urllib.request.Request(_TENCENT_URL % HK_SYMBOL,
                                     headers={"User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=15, context=_CTX).read().decode(
            "utf-8", "ignore")
        node = ((json.loads(raw).get("data") or {}).get("hk" + HK_SYMBOL) or {})
        lines = node.get("qfqday") or node.get("day") or []
    except Exception:
        return 0
    rows = []
    for it in lines:
        if len(it) < 3:
            continue
        try:
            d = str(it[0])                       # 腾讯港股日K 日期已是 YYYY-MM-DD
            if len(d) != 10 or d < _DATA_BEG:
                continue
            rows.append((HK_SYMBOL, d, float(it[2]), "tencent_kline"))
        except (ValueError, TypeError, IndexError):
            continue
    c = _db(db)
    try:
        _ensure_tables(c)
        if rows:
            c.executemany("""INSERT OR REPLACE INTO index_history
                             (symbol, date, close, source) VALUES (?, ?, ?, ?)""", rows)
        _mark_ok(c, "hk_daily", HK_SYMBOL, datetime.datetime.now())
        c.commit()
        return len(rows)
    finally:
        c.close()


def ensure_hk_daily(db=None):
    """过期即补（12h TTL）。返回本轮抓取行数（0=未到期或失败）。"""
    now_ts = datetime.datetime.now()
    c = _db(db)
    try:
        _ensure_tables(c)
        if not _stale(c, "hk_daily", HK_SYMBOL, now_ts):
            return 0
    finally:
        c.close()
    return fetch_hk_daily(db)


# ---------------------------------------------------------------------------
# 驱动序列
# ---------------------------------------------------------------------------
def hk_series(db=None):
    """{date: 02830 港股收盘}。"""
    c = _db(db)
    try:
        _ensure_tables(c)
        return {d: float(v) for d, v in c.execute(
            "SELECT date, close FROM index_history WHERE symbol=? AND close>0 "
            "ORDER BY date", (HK_SYMBOL,))}
    finally:
        c.close()


def fx_series(db=None):
    """{date: 港币兑人民币中间价}（exchange_rate.hkd_cny_mid）。"""
    c = _db(db)
    try:
        return {d: float(v) for d, v in c.execute(
            "SELECT date, %s FROM exchange_rate WHERE %s IS NOT NULL ORDER BY date"
            % (FX_COL, FX_COL))}
    finally:
        c.close()


def _ob(series, date):
    """series 里 ≤ date 的最近一个交易日（series 为 date→value 升序 dict）。"""
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return best


def driver_ratio(hk, fx, anchor, t):
    """driver(t)/driver(anchor) = [hk(t)/hk(anchor)] × [fx(t)/fx(anchor)]。

    两侧均用「≤ 该日的最近值」，避免港/中交易日历差（如国庆期间港交所照常）
    导致缺值。任一序列缺失返回 None（宁空勿错，不兜底）。"""
    ha, ht = _ob(hk, anchor), _ob(hk, t)
    fa, ft = _ob(fx, anchor), _ob(fx, t)
    if not (ha and ht and fa and ft):
        return None
    if hk[ha] <= 0 or hk[ht] <= 0 or fx[fa] <= 0 or fx[ft] <= 0:
        return None
    return (hk[ht] / hk[ha]) * (fx[ft] / fx[fa])


def _nav_rows(code, db=None):
    c = _db(db)
    try:
        return [(d, float(v)) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
    finally:
        c.close()


def _realtime_hk_price():
    """02830 港股实时现价（新浪 rt_hk，实时口）。取不到返回 None（不兜底）。"""
    try:
        from core.hk_quote import HKQuoteFetcher
        m = HKQuoteFetcher().get_realtime_marks([HK_SYMBOL])
        v = m.get(HK_SYMBOL)
        if v and v[0]:
            return float(v[0])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 估值入口
# ---------------------------------------------------------------------------
def estimate_t1(code, db=None):
    """主看板「T-1 静态估值」：以最新公告净值为锚，向前投影一步到 T-1。

    anchor = 最新公告净值日（T-2）；t1 = anchor 之后第一个有 02830 收盘的交易日；
    est = NAV(anchor) × driver(t1)/driver(anchor)。

    返回 dict（对齐 core.static_val.latest_static 消费字段）：
        {static_val, actual, nav_prev, nav_date, as_of_date, driver_ratio, method}
    """
    if code not in FUNDS:
        return {"static_val": None, "trustworthy": False, "note": "非沙特双雄"}
    try:
        ensure_hk_daily(db)
    except Exception:
        pass
    navs = _nav_rows(code, db)
    if not navs:
        return {"static_val": None, "trustworthy": False, "note": "无净值"}
    nav_map = dict(navs)
    anchor = navs[-1][0]
    hk = hk_series(db)
    fx = fx_series(db)
    after = [d for d in sorted(hk) if d > anchor]
    if not after:
        return {"static_val": None, "trustworthy": False, "note": "无 T-1 驱动数据"}
    t1 = after[0]
    r = driver_ratio(hk, fx, anchor, t1)
    if r is None:
        return {"static_val": None, "trustworthy": False, "note": "汇率缺失"}
    est = nav_map[anchor] * r
    actual = nav_map.get(t1)
    return {
        "static_val": round(est, 4),
        "actual": actual,
        "nav_prev": nav_map[anchor],
        "nav_date": anchor,
        "as_of_date": t1,
        "driver_ratio": round(r, 6),
        "method": "hk02830_bridge",
        "trustworthy": True,
    }


def realtime_estimate(code, db=None):
    """主看板实时估值：NAV(anchor) × driver(now)/driver(anchor)，now=02830 实时价。

    返回 dict：{static_val, nav_prev, nav_date, hk_now, hk_anchor, fx_now, fx_anchor}
    """
    if code not in FUNDS:
        return {"static_val": None, "trustworthy": False, "note": "非沙特双雄"}
    try:
        ensure_hk_daily(db)
    except Exception:
        pass
    navs = _nav_rows(code, db)
    if not navs:
        return {"static_val": None, "trustworthy": False, "note": "无净值"}
    nav_map = dict(navs)
    anchor = navs[-1][0]
    hk = hk_series(db)
    fx = fx_series(db)
    px = _realtime_hk_price()
    ha = _ob(hk, anchor)
    fa = _ob(fx, datetime.date.today().strftime("%Y-%m-%d"))
    if px is None or not ha or not fa:
        return {"static_val": None, "trustworthy": False, "note": "实时源缺失"}
    est = nav_map[anchor] * (px / hk[ha]) * (fx[fa] / fx[_ob(fx, anchor)])
    return {
        "static_val": round(est, 4),
        "nav_prev": nav_map[anchor],
        "nav_date": anchor,
        "hk_now": px,
        "hk_anchor": hk[ha],
        "fx_now": fx[fa],
        "fx_anchor": fx[_ob(fx, anchor)],
        "method": "hk02830_rt",
        "trustworthy": True,
    }


def static_history(code, days=60, db=None):
    """历史页行（升序）：static_val(D) = NAV(D-1) × driver(D)/driver(D-1)。

    行结构对齐 core.static_val.static_history：[{date, nav, static_val, err_pct}]。
    数据不足（驱动缺）的日期不出行 —— 宁空勿错。
    """
    if code not in FUNDS:
        return []
    try:
        ensure_hk_daily(db)
    except Exception:
        pass
    navs = _nav_rows(code, db)
    if len(navs) < 2:
        return []
    hk = hk_series(db)
    fx = fx_series(db)
    out = []
    for i in range(1, len(navs)):
        d, d0 = navs[i][0], navs[i - 1][0]
        r = driver_ratio(hk, fx, d0, d)
        if r is None:
            continue
        st = navs[i - 1][1] * r
        if st <= 0:
            continue
        out.append({"date": d, "nav": navs[i][1], "static_val": round(st, 4),
                    "err_pct": (navs[i][1] / st - 1) * 100})
    return out[-days:]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    try:
        sys.path.insert(0, "core")
    except Exception:
        pass
    n = fetch_hk_daily()
    print("hk02830 日K写入行数: %s" % n)
    for cc in FUNDS:
        print("== %s T-1 ==" % cc, estimate_t1(cc))
        print("   实时:", realtime_estimate(cc))
        for r in static_history(cc, days=6)[-6:]:
            print("   %s nav=%.4f static=%.4f err=%+.3f%%" %
                  (r["date"], r["nav"], r["static_val"], r["err_pct"]))
