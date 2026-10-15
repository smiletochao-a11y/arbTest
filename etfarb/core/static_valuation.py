# -*- coding: utf-8 -*-
"""
core/static_valuation.py

ETF 静态估值 / 实时 IOPV 通用引擎（同 LOF 估值思路：先静态验证算法，再实时化）。

核心方法：指数比例法
    static_est_t = nav_{t-1} * (index_t / index_{t-1}) * fx_factor
    fx_factor = fx_t / fx_{t-1}        # QDII 汇率因子（可选）

验证结论（2026-08-31，库内真实数据）：
    - 161125（标普500 QDII，单层跟踪）对标普500(.INX) 回测 σ=8.3bp  → 算法成立
    - 159687（亚太精选 QDII，双层 FoF）对 GPCSP006     回测 σ=111bp → 结构属性，非算法问题

关于汇率因子（重要，避免想当然）：
    理论上 QDII 净值 = 指数 × 汇率，应带 fx_factor。但实测 161125 加 usd_cny_spot 后
    σ 反而 8.3→13bp（FX 历史仅 5 天且 spot 与基金使用的托管行汇率时点不一致，引入噪声）。
    故当前两个标的 fx_col 均设为 None，用纯指数比例法（已验证到噪声地板）。待有基金
    实际汇率源时再开启。模块已保留 fx 能力，开箱即用。

新增 ETF 只需在 ETF_BENCHMARKS 加一行，无需动调用方。
"""

import sqlite3
import statistics
import os as _os
import sys as _sys
from datetime import datetime

# ETFarb 数据层：单库方案，复用 core/db.get_conn() → 共享 arb_master.db 的 unified_fund_history
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
from core.db import get_conn

# 标的 → 基准配置（唯一权威）
ETF_BENCHMARKS = {
    "159687": {
        "name": "南方东英富时亚太精选ETF(QDII)",
        "index": "GPCSP006",          # 富时亚太精选指数（SGX 上市，东财 secid=249.GPCSP006）
        "secid": "249.GPCSP006",      # 东财实时指数 secid
        "idx_scale": 100,             # 东财 f43 以 ×100 整数返回
        "structure": "fof",           # fof=双层基金中基金；single=单层跟踪
        "fx_col": None,               # SGX ETF 计价复杂，暂用纯指数比例法
        "acceptable_sigma": 150,      # FoF 双层误差天然在 100bp 级，放宽阈值
    },
    "159329": {
        "name": "南方基金沙特ETF(QDII)",
        "index": "FISAULMU",          # 富时沙特阿拉伯指数（东财 secid=100.FISAULMU）
        "secid": "100.FISAULMU",      # 东财实时指数 secid（已搜得；沙箱偶发连不上，本机可用）
        "idx_scale": 100,             # 同 GPCSP006 假设 ×100；本机首次运行请核对显示点位
        "structure": "single",        # 单层跟踪富时沙特指数
        "fx_col": None,               # 暂无稳定汇率源，暂用纯指数比例法
        "acceptable_sigma": 50,       # 单层 QDII，未回测，暂沿用 161125 口径
    },
    "510300": {
        "name": "沪深300ETF华泰柏瑞",
        "index": "SH000300",          # 沪深300指数（深交所，腾讯接口 sh000300）
        "secid": "sh000300",          # 腾讯接口代码
        "idx_scale": 100,             # 腾讯指数 ×100
        "structure": "single",
        "fx_col": None,
        "acceptable_sigma": 50,
        "data_source": "tencent",     # 标记使用腾讯数据源（非东财）
    },
}


def _db():
    return get_conn()


def get_latest_nav(fund_code):
    """返回 (nav, date) 最新公告净值。"""
    con = _db()
    row = con.execute(
        "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL ORDER BY date DESC LIMIT 1",
        (fund_code,),
    ).fetchone()
    con.close()
    return (float(row[1]), row[0]) if row else (None, None)


def get_index_on(fund_code, date):
    """返回该基金基准指数在 date（含 date 之前最近一日）的收盘。"""
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return None
    con = _db()
    row = con.execute(
        "SELECT close FROM index_history WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
        (cfg["index"], date),
    ).fetchone()
    con.close()
    return float(row[0]) if row else None


def get_latest_index(fund_code):
    """返回该基金基准指数最新值 (close, date)。"""
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return None, None
    con = _db()
    row = con.execute(
        "SELECT close, date FROM index_history WHERE symbol=? ORDER BY date DESC LIMIT 1",
        (cfg["index"],),
    ).fetchone()
    con.close()
    return (float(row[0]), row[1]) if row else (None, None)


def _fx_factor(fund_code, date_prev, date_today):
    """返回 fx_today / fx_prev；无 fx_col 或缺失则 1.0。"""
    cfg = ETF_BENCHMARKS.get(fund_code)
    fx_col = cfg.get("fx_col") if cfg else None
    if not fx_col:
        return 1.0
    con = _db()
    rp = con.execute("SELECT %s FROM exchange_rate WHERE date<=? ORDER BY date DESC LIMIT 1" % fx_col, (date_prev,)).fetchone()
    rt = con.execute("SELECT %s FROM exchange_rate WHERE date<=? ORDER BY date DESC LIMIT 1" % fx_col, (date_today,)).fetchone()
    con.close()
    if rp and rt and rp[0] and rt[0]:
        return float(rt[0]) / float(rp[0])
    return 1.0


def estimate_iopv(fund_code, idx_today=None, idx_today_date=None):
    """估算实时 IOPV（盘中静态估值）。

    参数 idx_today / idx_today_date：基准指数今日实时价及其日期。
        为 None 时自动取库里该指数最新值（适用于已有实时采集的标的）。
    返回 dict：含 static_val、nav_prev、idx_prev、idx_today、fx_factor、
        err_vs_nav_bp（与最新公告净值比，仅参考）、trustworthy、note。
    """
    nav_prev, nav_date = get_latest_nav(fund_code)
    if not nav_prev:
        return {"static_val": None, "trustworthy": False, "note": "无净值"}
    idx_prev = get_index_on(fund_code, nav_date)
    if idx_today is None:
        idx_today, idx_today_date = get_latest_index(fund_code)
    if not idx_prev or not idx_today:
        return {"static_val": None, "trustworthy": False, "note": "指数缺失"}
    fx = _fx_factor(fund_code, nav_date, idx_today_date or datetime.now().strftime("%Y-%m-%d"))
    est = nav_prev * (idx_today / idx_prev) * fx

    con = _db()
    row = con.execute(
        "SELECT nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL ORDER BY date DESC LIMIT 1",
        (fund_code,),
    ).fetchone()
    con.close()
    err = (est - float(row[0])) / float(row[0]) * 10000 if row else None

    return {
        "static_val": round(est, 4),
        "nav_prev": nav_prev,
        "nav_date": nav_date,
        "idx_prev": idx_prev,
        "idx_today": idx_today,
        "idx_today_date": idx_today_date,
        "fx_factor": fx,
        "err_vs_nav_bp": round(err, 1) if err is not None else None,
        "trustworthy": True,
        "note": "",
    }


def estimate_t1_lcu(fund_code, as_of_date=None):
    """LCU 桥接 T-1 估值腿（159687 专用增强入口，东哥 2026-10-08 拍板口径）。

    算法（向前投影一步）：以最新公告净值 anchor(=T-2) 为锚，投影到其后的第一个
    有驱动值的交易日 t1(=T-1)：
        est   = NAV[anchor] × driver_ratio
        driver_ratio = LCU[t1]/LCU[anchor]           优先（LCU 已吸收 FoF/汇率/时区噪声）
                     GPCSP006[t1]/GPCSP006[anchor]   兜底

    语义修正（vs 旧回测）：旧版 est = NAV[T-3] × LCU[T-2]/LCU[T-3]，只是把最新已知
    净值再推一遍，假期断更时冻在旧值；新版给出「最新已知净值 → 下一日(T-1)」的前瞻估值，
    与大山口径对齐（例：9-29 净值 → 9-30 估值 = 1.8692 ≈ 大山 1.8691）。
    anchor 之后无任何驱动数据时才退回旧回测语义兜底，避免空值。
    红线：本函数只读、不写库。
    """
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return {"static_val": None, "trustworthy": False, "note": "无基准配置"}
    con = _db()
    nav_rows = con.execute(
        "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL ORDER BY date",
        (fund_code,),
    ).fetchall()
    idx = dict(con.execute(
        "SELECT date, close FROM index_history WHERE symbol=?", (cfg["index"],)
    ).fetchall())
    con.close()

    if not nav_rows:
        return {"static_val": None, "trustworthy": False, "note": "无净值"}
    nav_map = {d: float(n) for d, n in nav_rows}
    dates = sorted(nav_map)
    # 锚定日 anchor = 最新公告净值日（T-2）；as_of_date 显式传入时即作为 anchor。
    if as_of_date is None:
        anchor = dates[-1]
    else:
        if as_of_date not in nav_map:
            cand = [d for d in dates if d < as_of_date]
            if not cand:
                return {"static_val": None, "trustworthy": False, "note": "无更早净值"}
            as_of_date = cand[-1]
        anchor = as_of_date

    from core import lcu_nav_source as lcu
    lcu_src = lcu.lcu_source()
    lcu_dates = [d for d in lcu_src.daterange() if d > anchor]
    idx_dates = [d for d in sorted(idx) if d > anchor]

    # 向前投影一步到 T-1：anchor 与 t1 两日均需有驱动值；LCU 优先，GPCSP006 兜底。
    t1 = None
    driver_ratio = None
    method = None
    if lcu_dates and lcu_src.get(anchor) is not None:
        _t = lcu_dates[0]
        if lcu_src.get(_t) is not None:
            t1 = _t
            driver_ratio = lcu_src.get(_t) / lcu_src.get(anchor)
            method = "lcu_bridge"
    if method is None and idx_dates:
        _t = idx_dates[0]
        a = idx.get(_t)
        b = idx.get(anchor)
        if a and b:
            t1 = _t
            driver_ratio = a / b
            method = "index_fallback"

    if method is not None:
        nav_anchor = nav_map[anchor]
        est = nav_anchor * driver_ratio
        actual = nav_map.get(t1)            # T-1 真实净值（已公布则为核对基准，否则 None）
        err = (est - actual) / actual * 10000 if actual else None
        return {
            "static_val": round(est, 4),
            "actual": actual,
            "nav_prev": nav_anchor,
            "nav_date": anchor,
            "as_of_date": t1,
            "driver_ratio": round(driver_ratio, 6),
            "method": method,
            "err_vs_actual_bp": round(err, 1) if err is not None else None,
            "trustworthy": True,
        }

    # 兜底：anchor 之后无驱动数据 → 退回「用 anchor 前一日回测 anchor 当天」（旧语义）
    prev_dates = [d for d in dates if d < anchor]
    if not prev_dates:
        return {"static_val": None, "trustworthy": False, "note": "LCU 与指数均无后续数据"}
    dp = prev_dates[-1]
    nav_prev = nav_map[dp]
    actual = nav_map[anchor]
    lr = lcu.lcu_change(anchor, dp)
    if lr is not None:
        driver_ratio = lr
        method = "lcu_bridge"
    else:
        a = idx.get(anchor)
        b = idx.get(dp)
        if a and b:
            driver_ratio = a / b
            method = "index_fallback"
        else:
            return {"static_val": None, "trustworthy": False,
                    "note": "LCU 与指数均缺失", "nav_prev": nav_prev, "nav_date": dp}
    est = nav_prev * driver_ratio
    err = (est - actual) / actual * 10000
    return {
        "static_val": round(est, 4),
        "actual": actual,
        "nav_prev": nav_prev,
        "nav_date": dp,
        "as_of_date": anchor,
        "driver_ratio": round(driver_ratio, 6),
        "method": method,
        "err_vs_actual_bp": round(err, 1),
        "trustworthy": True,
    }


def history_series(fund_code, limit=30):
    """返回该基金历史逐日静态估值序列（用于历史弹窗）。

    逐日用 prev-day 比例法：est_d = nav_{d-1} * idx_d / idx_{d-1}。
    返回 list[{date, nav, price, idx, static_val, premium, val_error}]（升序）。
    """
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return []
    con = _db()
    nav_rows = con.execute(
        "SELECT date, nav, price FROM unified_fund_history WHERE fund_code=? ORDER BY date DESC LIMIT ?",
        (fund_code, limit),
    ).fetchall()
    idx_map = dict(con.execute(
        "SELECT date, close FROM index_history WHERE symbol=?", (cfg["index"],)
    ).fetchall())
    # LCU 桥接序列（159687 专属）：该基金几乎纯持 LCU，用 LCU 序列投影比 GPCSP006 指数法准（σ 4bp）
    lcu_map = {}
    try:
        lcu_map = dict(con.execute(
            "SELECT date, nav FROM lcu_nav_history"
        ).fetchall())
    except Exception:
        lcu_map = {}
    con.close()

    # 锚点：最新有净值的日（DESC 取首个有 nav 的），用于 LCU 桥接投影
    anchor_nav = anchor_date = None
    for d, nv, _ in nav_rows:
        if nv is not None:
            anchor_nav, anchor_date = nv, d
            break

    nav_rows = list(reversed(nav_rows))  # 升序
    out = []
    prev = None
    for date, nav, price in nav_rows:
        idx = idx_map.get(date)
        static_val = None
        val_error = None
        # 优先 LCU 桥接：以最新净值为锚，用 LCU 序列投影（与主看板 T-1 列同口径）
        if lcu_map and anchor_nav is not None and date in lcu_map and anchor_date in lcu_map:
            static_val = round(anchor_nav * (lcu_map[date] / lcu_map[anchor_date]), 4)
        # 兜底：无 LCU 序列时用 prev-day 比例法（GPCSP006 指数法）
        elif prev and idx and prev["idx"] and prev["nav"]:
            static_val = round(prev["nav"] * (idx / prev["idx"]), 4)
        if static_val is not None and nav is not None:
            val_error = round((static_val - nav) / nav * 100, 4)
        premium = round((price / nav - 1) * 100, 2) if (price and nav) else None
        out.append({
            "date": date, "nav": nav, "price": price, "idx": idx,
            "static_val": static_val, "premium": premium, "val_error": val_error,
        })
        if nav is not None:
            prev = {"nav": nav, "idx": idx}
    return out


def backtest(fund_code, with_fx=None):
    """回测静态估值误差 vs 公告净值，验证算法合理性。

    with_fx: None=按配置（有 fx_col 则用），True/False 强制开关。
    返回 dict：{n, sigma_bp, mean_bp, max_bp, min_bp, ratio_median, with_fx,
               acceptable_sigma, pass}；无数据返回 None。
    """
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return None
    con = _db()
    nav = dict(con.execute(
        "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL",
        (fund_code,),
    ).fetchall())
    idx = dict(con.execute(
        "SELECT date, close FROM index_history WHERE symbol=?", (cfg["index"],)
    ).fetchall())
    fx = {}
    if cfg.get("fx_col"):
        fx = dict(con.execute(
            "SELECT date, %s FROM exchange_rate WHERE %s IS NOT NULL" % (cfg["fx_col"], cfg["fx_col"])
        ).fetchall())
    con.close()

    dates = sorted(set(nav) & set(idx))
    use_fx = (cfg.get("fx_col") is not None) if with_fx is None else with_fx

    errs = []
    ratios = []  # 净值日变化率 / 指数日变化率
    for i in range(1, len(dates)):
        d, dp = dates[i], dates[i - 1]
        est = nav[dp] * (idx[d] / idx[dp])
        if use_fx and dp in fx and d in fx:
            est *= (fx[d] / fx[dp])
        act = nav[d]
        errs.append((est - act) / act * 10000)
        ic = idx[d] / idx[dp] - 1
        nc = nav[d] / nav[dp] - 1
        if abs(ic) > 1e-9:
            ratios.append(nc / ic)

    if not errs:
        return None
    return {
        "n": len(errs),
        "sigma_bp": round(statistics.pstdev(errs), 1),
        "mean_bp": round(statistics.mean(errs), 1),
        "max_bp": round(max(errs), 1),
        "min_bp": round(min(errs), 1),
        "ratio_median": round(statistics.median(ratios), 3) if ratios else None,
        "with_fx": use_fx,
        "acceptable_sigma": cfg["acceptable_sigma"],
        "pass": statistics.pstdev(errs) <= cfg["acceptable_sigma"],
    }
