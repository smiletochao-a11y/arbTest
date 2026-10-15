"""ETFarb 净值采集：复用 arbcore 纯取数函数，结果写 arb_master.db 共享宽表。

设计要点（东哥 2026-09-02 确立，2026-10-05 同库化）：
- 不重写东财 lsjz 解析逻辑——直接复用 arbcore.fetchers.historical.eastmoney
  .EastMoneyHistoricalFetcher.fetch_nav（纯 HTTP 取数、无写库副作用）。
- 取回的净值写共享库 arb_master.db 的 unified_fund_history（与 arbTest 同库，
  按 in_arbtest 分区写，ETFarb 写自己 366 只的 nav 列）。
- 指数历史（GPCSP006/FISAULMU）不在此模块，走 backfill 脚本（push2his）。
"""
import os
import sys
import datetime

# 确保能从任意目录 import core.db（进而注入 arbTest/src 到 sys.path）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.db as db
from arbcore.fetchers.historical.eastmoney import EastMoneyHistoricalFetcher

_fetcher = EastMoneyHistoricalFetcher()


def fetch_and_save(fund_code, days=400):
    """抓取 fund_code 的东财历史净值并写入 unified_fund_history.nav。返回写入行数。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    df = _fetcher.fetch_nav(fund_code, start_date=start.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return 0
    conn = db.get_conn()
    n = 0
    try:
        for _, r in df.iterrows():
            d = r["date"]
            ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 nav 是谁写的"（凌晨批来源排查教训）
            db.upsert_fund_history(fund_code, ds, {"nav": float(r["nav"])},
                                   source="eastmoney", conn=conn, writer="etfarb:nav_fetcher")
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


if __name__ == "__main__":
    codes = sys.argv[1:] or ["159687", "159329"]
    for c in codes:
        n = fetch_and_save(c)
        print(f"{c}: 写入 {n} 行净值 -> {db.DB_PATH}")
