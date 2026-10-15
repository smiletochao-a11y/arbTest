# -*- coding: utf-8 -*-
"""历史核对页数据装配（2026-09-19 东哥要求 UI 完全对齐 LOF 套利程序）。

统一入口 `get_history(code, ...)`：
  · 现金管理 3 只（val_method=bond）→ bond_etf_valuation.static_history_bond
    （列：日期/净值/每日增加[前端算]/收盘价/折价几根毛/溢价/指数或期货/静态估值/误差）
    · 国内 LOF（lof_index）→ static_val.static_history + 富化：
    收盘价(新浪日K)、nav_chg、静态溢价=(price/static_val−1)、指数价/指数涨幅
    （index_history，缺源时 A 股指数新浪日K兜底回写，同 000012 先例）、
    份额/新增（unified_fund_history.shares/shares_added，VPS 万份口径）。

行统一降序；数据不足的字段留 None（前端显 '—'），宁空勿错。
`refresh=True` = 前端「核对静态估值（补采+重算）」按钮：清缓存强制回源。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import static_val as _sv                     # noqa: E402
import bond_etf_valuation as _bv             # noqa: E402
try:
    import saudi_bridge as _saudi            # 沙特双雄驱动（02830 × HKD/CNY）
except ImportError:
    from core import saudi_bridge as _saudi
from core.db import DB_PATH                   # noqa: E402

DATA_BEG = "2024-01-01"


def _driver_meta(code):
    """历史页「驱动」两列元信息 → (显示名, 单位, 小数位)。

    · 159687 → ('LCU', '净值', 4)：LCU 官方 NAV 桥接（前端显「LCU 净值 / LCU 涨幅」）
    · 520830/159329 → ('02830', '价', 2)：港股 02830 收盘价（「02830 价 / 02830 涨幅」）
    · 其余 → (None, None, 4)：无驱动列，回落到「指数价 / 指数涨幅」。
    """
    if code == '159687':
        return ('LCU', '净值', 4)
    if code in _saudi.FUNDS:
        return ('02830', '价', 2)
    return (None, None, 4)


def _profile(code, db):
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        r = c.execute(
            "SELECT fund_code, fund_name, jsl_index_nm, idx_code, val_method, fund_type, "
            "apply_fee, redeem_fee FROM fund_profile WHERE fund_code=?", (code,)).fetchone()
        if not r:
            return None
        keys = ["fund_code", "fund_name", "jsl_index_nm", "idx_code", "val_method",
                "fund_type", "apply_fee", "redeem_fee"]
        return dict(zip(keys, r))
    finally:
        if own:
            c.close()


def _nav_series(code, db):
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        return [(d, v) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
    finally:
        if own:
            c.close()


def _share_series(code, db):
    """{date: {shares, shares_added}}（unified_fund_history，VPS 万份口径）。"""
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        out = {}
        for d, s, n in c.execute(
                "SELECT date, shares, shares_added FROM unified_fund_history "
                "WHERE fund_code=? AND shares>0 ORDER BY date", (code,)):
            out[d] = {"shares": s, "shares_added": n}
        return out
    finally:
        if own:
            c.close()


def _sina_index_fallback(cs, db):
    """A 股指数新浪日K兜底（腾讯 fqkline 501 时用），回写 index_history。

    与 bond_etf_valuation._treasury_series 的 000012 先例同一哲学：
    消费侧只认 index_history 一张表，兜底写回同表并标 source='sina_kline'。"""
    sym = ("sz" if cs.startswith(("399", "159")) else "sh") + cs
    rows = _bv._fetch_sina_kline(sym)
    if len(rows) < 2:
        return
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        c.executemany(
            """INSERT OR REPLACE INTO index_history (symbol, date, close, source)
               VALUES (?, ?, ?, 'sina_kline')""",
            [(cs, d, v) for d, v in rows.items() if d >= DATA_BEG])
        c.commit()
    finally:
        if own:
            c.close()


def _index_series(idx_code, db, refresh=False):
    """跟踪指数 {date: close}（历史通道裸码口径；A 股指数缺源新浪兜底）。"""
    cs = _sv._hist_symbol(idx_code) if idx_code else None
    if not cs:
        return {}
    if refresh:
        try:
            _sv.fetch_index_history(idx_code, db)
        except Exception:
            pass
    else:
        try:
            _sv.ensure_index_history([idx_code], db)
        except Exception:
            pass
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        rows = {d: v for d, v in c.execute(
            "SELECT date, close FROM index_history WHERE symbol=? AND date>=? "
            "ORDER BY date", (cs, DATA_BEG))}
    finally:
        if own:
            c.close()
    if len(rows) < 2 and cs.startswith(("0", "3")):
        _sina_index_fallback(cs, db)
        c = sqlite3.connect(db) if isinstance(db, str) else db
        own = c is not db
        try:
            rows = {d: v for d, v in c.execute(
                "SELECT date, close FROM index_history WHERE symbol=? AND date>=? "
                "ORDER BY date", (cs, DATA_BEG))}
        finally:
            if own:
                c.close()
    return rows


def _close_on_or_before(series, date):
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return (best, series[best]) if best else (None, None)


def _history_lof(code, days, refresh, db):
    """LOF 历史行（降序）：static_val 引擎 + 价格/指数/份额富化。"""
    p = _profile(code, db) or {}
    idx_code = p.get("idx_code")
    # 净值补采：refresh=尽力回源；否则过期即补（均不致命，本地数据仍可读）
    try:
        if refresh:
            _sv.fetch_nav_history(code, db)
        else:
            _sv.ensure_nav_history([code], db)
    except Exception:
        pass
    # 🔴 先 ensure 指数序列（含 A 股指数新浪兜底回写），static_history 内部
    #   直接读 index_history —— 顺序反了会拿到空序列而整页 0 行。
    #   refresh 时 _index_series 内部已尽力回源（不致命），勿再重复抓取。
    idx_series = _index_series(idx_code, db, refresh) if idx_code else {}
    # 驱动序列（历史页「驱动 净值/价 + 涨幅」两列）：
    #   · 159687 → LCU 官方 NAV（LCU 桥接）
    #   · 520830/159329 → 港股 02830 收盘价（沙特桥接，估值驱动 = 02830 × HKD/CNY）
    drv_name, drv_unit, drv_dec = _driver_meta(code)
    drv_series = {}
    try:
        if code == '159687':
            lc = sqlite3.connect(db) if isinstance(db, str) else db
            own_lc = lc is not db
            lcu_rows = lc.execute(
                "SELECT date, nav FROM lcu_nav_history ORDER BY date").fetchall()
            if own_lc:
                lc.close()
            if len(lcu_rows) >= 2:
                drv_series = {d: v for d, v in lcu_rows}
        elif code in _saudi.FUNDS:
            drv_series = _saudi.hk_series(db)
    except Exception:
        drv_series = {}
    drv_dates = sorted(drv_series)
    base = _sv.static_history(code, days=days, db=db)   # 升序
    if not base:
        # 🔴 降级分支（2026-09-27）：指数历史缺失（无 idx_code / 无免费指数源，
        #   如 WATCH 池的美股指数 NDX/DJI/SP500、中证自定义 2.93xxxx、GPCSP006）
        #   ⇒ static_history 返回 []。但「净值 / 份额 / 新增」是真实数据，
        #   不应被指数缺失绑架整页空白。降级为「以净值序列为骨架」出行，
        #   指数与静态估值列留空（宁空勿错，行照常）。
        navs0 = _nav_series(code, db)
        base = [{"date": d, "nav": v, "static_val": None, "err_pct": None}
                for d, v in navs0]
    if not base:
        return []
    navs = _nav_series(code, db)
    nav_d = dict(navs)
    nav_arr = [d for d, _ in navs]
    # 价格（新浪日K）：失败不致命 → 价格/涨跌幅列留空，行照常（宁空勿错）
    try:
        px = _bv.fetch_fund_kline(code) or {}
    except Exception:
        px = {}
    px_dates = sorted(px)
    px_chg = {}
    for j, pd in enumerate(px_dates):
        prev_px = px[px_dates[j - 1]] if j > 0 and px[px_dates[j - 1]] else None
        px_chg[pd] = (px[pd], (px[pd] / prev_px - 1) * 100 if prev_px else None)
    shares = _share_series(code, db)

    idx_dates = sorted(idx_series) if idx_series else []

    out = []
    for r in base:
        d = r["date"]
        # nav_chg：全 nav 序列里的上一公布日
        i = nav_arr.index(d) if d in nav_arr else None
        nav_chg = None
        if i is not None and i > 0 and navs[i - 1][1]:
            nav_chg = (navs[i][1] / navs[i - 1][1] - 1) * 100
        price, price_chg = px_chg.get(d, (None, None))
        sv = r["static_val"]
        row = {
            "date": d, "nav": r["nav"], "nav_chg": nav_chg,
            "price": price, "price_chg": price_chg,
            "static_val": sv,
            "val_error": (sv - r["nav"]) if (sv is not None and r["nav"]) else None,
            "val_error_rate": ((sv - r["nav"]) / r["nav"] * 100)
                              if (sv is not None and r["nav"]) else None,
            "static_premium": (price / sv - 1) * 100 if (price and sv) else None,
        }
        if idx_series:
            ib, ib_px = _close_on_or_before(idx_series, d)
            if ib_px:
                prev_list = [x for x in idx_dates if x < ib]
                row["index_close"] = ib_px
                row["index_pct"] = (ib_px / idx_series[prev_list[-1]] - 1) * 100 \
                    if prev_list and idx_series[prev_list[-1]] else None
        # 驱动列：159687=LCU 净值 / 沙特双雄=02830 港股价（前端按 driver_name 显标题）
        if drv_series:
            ib, ib_px = _close_on_or_before(drv_series, d)
            if ib_px is not None:
                prev_list = [x for x in drv_dates if x < ib]
                _pct = (ib_px / drv_series[prev_list[-1]] - 1) * 100 \
                    if prev_list and drv_series[prev_list[-1]] else None
                row["drv_close"] = ib_px
                row["drv_pct"] = _pct
                # 向后兼容：ARM 上 index.html 若仍是旧版（只认 uses_lcu/lcu_*），
                # 159687 的 LCU 两列照常出数（新前端用 driver_name/drv_*）。
                if code == '159687':
                    row["lcu_close"] = ib_px
                    row["lcu_pct"] = _pct
        sh = shares.get(d)
        if sh:
            row["shares"] = sh["shares"]
            row["shares_added"] = sh["shares_added"]
        out.append(row)
    return list(reversed(out))[-days:]


_CASH_DESC = ("债券型：静态估值(D) = 前一日净值 + 日票息 + 方向项(D)。"
              "511880=日均增长法；511360=票息+国债指数000012×0.07；"
              "511520=票息+T2609期货（缺失日退化国债指数代理）。")
_LOF_DESC = ("静态估值 = 前一日净值 × (1 + 0.95 × 指数涨幅)。"
             "它是基金公司公布净值**之前**的预估 —— 用它和随后公布的 Nav 核对答案，"
             "「误差」越小说明估值算法越准。")
_SAUDI_DESC = ("沙特双雄：静态估值 = 前一日净值 × 02830 港股涨跌 × 港币汇率涨跌。"
               "两只基金 100% 持有港股 02830（南方东英沙特阿拉伯ETF），"
               "故用其港股收盘价 × 港币中间价还原净值 —— 与官方口径同源，误差远小于纯指数法。")


def get_history(code, days=60, refresh=False, db=None):
    """统一入口。返回 {code, name, kind: 'cash'|'lof', desc, profile, rows}。"""
    p = _profile(code, db) or {}
    if (p.get("val_method") == "bond") and code in _bv.BOND_ETF_META:
        rows = _bv.static_history_bond(code, days=days, db=db, refresh=refresh)
        return {"code": code, "name": p.get("fund_name") or "", "kind": "cash",
                "desc": _CASH_DESC,
                "profile": {"index_nm": None, "sub_fee": p.get("apply_fee"),
                            "redeem_fee": p.get("redeem_fee")},
                "rows": rows}
    rows = _history_lof(code, days, refresh, db)
    uses_lcu = any(r.get("drv_close") is not None for r in rows) and code == '159687'
    dn, du, dd = _driver_meta(code)
    desc = _SAUDI_DESC if code in _saudi.FUNDS else _LOF_DESC
    return {"code": code, "name": p.get("fund_name") or "", "kind": "lof",
            "desc": desc,
            "profile": {"index_nm": p.get("jsl_index_nm") or p.get("idx_name"),
                        "sub_fee": p.get("apply_fee"),
                        "redeem_fee": p.get("redeem_fee"),
                        "driver_name": dn, "driver_unit": du, "driver_dec": dd,
                        "uses_lcu": uses_lcu},
            "rows": rows}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    dbp = DB_PATH
    for cc in ("511880", "511360", "511520", "161725"):
        h = get_history(cc, days=5, db=dbp)
        print("==", cc, h["kind"], "rows=%d" % len(h["rows"]))
        for r in h["rows"][:2]:
            print("  ", r)
