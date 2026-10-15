# -*- coding: utf-8 -*-
"""
core/lcu_bridge_valuation.py

复刻「LCU 官方 NAV 桥接」算法，并对 159687 做交叉验证回测。

背景：
    159687（南方东英富时亚太精选ETF QDII）与 LCU（CSOP FTSE Asia Pacific Select
    Index ETF）跟踪同一标的（FTSE Asia Pacific Select）。159687 是境内上市、人民币
    计价的 QDII-FoF，估值带着 FoF 双层 / 汇率 / 时区错位噪声，纯指数法回测 σ≈111bp。

LCU 桥接算法（双轨）：
    T-1 估值  = 国内 T-2 净值 × LCU 官方净值两日变动   （底层 LCU 官方 NAV 驱动）
    T  日实时 = T-1 估值     × GPCSP006 实时涨跌        （指数驱动）
    若 LCU 未覆盖 → 降级用 GPCSP006 收盘桥接（即纯指数法）

本模块只做第一轨的逐日验证：用「国内 NAV[T-1] + LCU 两日变动」预测 NAV[T]，
并与「国内 NAV[T-1] + GPCSP006 两日变动」对比，看 LCU 桥接能把误差压低多少。

方法A（纯指数，当前生产引擎）：  est_T = NAV[T-1] × GPCSP006[T]/GPCSP006[T-1]
方法B（LCU 桥接）：               est_T = NAV[T-1] × LCU[T]/LCU[T-1]

红线：
    本模块只读、不改生产引擎（estimate_iopv 保持不动）、不写库。仅输出证据，
    供东哥决策是否把 LCU 桥接接进实时估值腿（作为 GPCSP006 的增强/兜底）。
"""

import sqlite3
import statistics
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.db import get_conn
from core import lcu_nav_source as lcu
from core.static_valuation import ETF_BENCHMARKS

DEFAULT_FUND = "159687"


def _nav_map(fund_code):
    con = get_conn()
    rows = con.execute(
        "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav IS NOT NULL",
        (fund_code,),
    ).fetchall()
    con.close()
    return {d: float(n) for d, n in rows}


def _idx_map(fund_code):
    cfg = ETF_BENCHMARKS.get(fund_code)
    if not cfg:
        return {}
    con = get_conn()
    rows = con.execute(
        "SELECT date, close FROM index_history WHERE symbol=?", (cfg["index"],)
    ).fetchall()
    con.close()
    return {d: float(c) for d, c in rows}


def cross_validate(fund_code=DEFAULT_FUND):
    """对 fund_code 跑 LCU 桥接 vs 纯指数的逐日回测。

    返回 dict：含两套误差统计（sigma/mean/n）与逐日明细 rows。
    每个 row：date, nav_prev_date, nav_prev, actual,
              est_a(GPCSP006), err_a_bp,
              lcu_chg, est_b(LCU桥接), err_b_bp
    """
    nav = _nav_map(fund_code)
    idx = _idx_map(fund_code)
    dates = sorted(nav.keys())

    rows = []
    errs_a, errs_b = [], []
    for i in range(1, len(dates)):
        d, dp = dates[i], dates[i - 1]
        nav_prev = nav[dp]
        actual = nav[d]

        # 方法A：GPCSP006 纯指数
        est_a = None
        if d in idx and dp in idx and idx[dp]:
            est_a = nav_prev * (idx[d] / idx[dp])
        # 方法B：LCU 官方 NAV 桥接
        chg = lcu.lcu_change(d, dp)
        est_b = nav_prev * chg if chg is not None else None

        e_a = (est_a - actual) / actual * 10000 if est_a else None
        e_b = (est_b - actual) / actual * 10000 if est_b else None
        if e_a is not None:
            errs_a.append(e_a)
        if e_b is not None:
            errs_b.append(e_b)

        rows.append({
            "date": d,
            "nav_prev_date": dp,
            "nav_prev": nav_prev,
            "actual": actual,
            "est_a": est_a,
            "err_a_bp": round(e_a, 1) if e_a is not None else None,
            "lcu_chg": round(chg, 5) if chg is not None else None,
            "est_b": est_b,
            "err_b_bp": round(e_b, 1) if e_b is not None else None,
        })

    return {
        "fund": fund_code,
        "n_a": len(errs_a),
        "sigma_a_bp": round(statistics.pstdev(errs_a), 1) if errs_a else None,
        "mean_a_bp": round(statistics.mean(errs_a), 1) if errs_a else None,
        "n_b": len(errs_b),
        "sigma_b_bp": round(statistics.pstdev(errs_b), 1) if errs_b else None,
        "mean_b_bp": round(statistics.mean(errs_b), 1) if errs_b else None,
        "rows": rows,
    }


def _fmt(v, nd=4):
    return ("%.*f" % (nd, v)) if v is not None else "  -  "


def print_report(res):
    print("=" * 92)
    print("159687 交叉验证回测：方法A=纯指数(GPCSP006)  vs  方法B=LCU桥接")
    print("=" * 92)
    print("方法A 样本=%d  σ=%.1fbp  均值=%.1fbp" % (res["n_a"], res["sigma_a_bp"], res["mean_a_bp"]))
    print("方法B 样本=%d  σ=%.1fbp  均值=%.1fbp" % (res["n_b"], res["sigma_b_bp"], res["mean_b_bp"]))
    if res["sigma_a_bp"] and res["sigma_b_bp"]:
        delta = res["sigma_a_bp"] - res["sigma_b_bp"]
        print("结论：LCU 桥接相对纯指数法 σ %s %.1fbp（%.0f%%）" % (
            "降低" if delta > 0 else "升高", abs(delta),
            abs(delta) / res["sigma_a_bp"] * 100))
    print("-" * 92)
    print("%-11s %-11s %8s %8s %8s %9s %8s %8s %9s" % (
        "date", "T-1", "navPrev", "actual", "estA", "errA_bp", "lcuChg", "estB", "errB_bp"))
    print("-" * 92)
    for r in res["rows"]:
        print("%-11s %-11s %8s %8s %8s %9s %8s %8s %9s" % (
            r["date"], r["nav_prev_date"],
            _fmt(r["nav_prev"]), _fmt(r["actual"]),
            _fmt(r["est_a"]), (("%.1f" % r["err_a_bp"]) if r["err_a_bp"] is not None else "  -  "),
            (("%.4f" % r["lcu_chg"]) if r["lcu_chg"] is not None else "  -  "),
            _fmt(r["est_b"]), (("%.1f" % r["err_b_bp"]) if r["err_b_bp"] is not None else "  -  ")))
    print("=" * 92)


if __name__ == "__main__":
    res = cross_validate(DEFAULT_FUND)
    print_report(res)
