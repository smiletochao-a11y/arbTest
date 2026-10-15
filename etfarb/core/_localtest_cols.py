# -*- coding: utf-8 -*-
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3
import etf_iopv_quote as eiq
import static_val as _sv

db = eiq.DB_PATH
# 清掉 idx_hist 的 TTL 标记（上轮东财抓 0 行也标了 ok，否则 12h 内不再重抓）
c = sqlite3.connect(db)
c.execute("DELETE FROM fetch_state WHERE src='idx_hist'")
c.commit(); c.close()

board = eiq.ETFMarketBoard()

print("=" * 70)
print("LOF 行（静态估值四列；港股指数改走腾讯日K线后应出数）")
print("=" * 70)
lo = board.build(fund_type="LOF", limit=8)
print("LOF rows=%d  stats=%s" % (len(lo["rows"]), lo["stats"]))
for r in lo["rows"]:
    print("  %s %-12s idx=%s idx_price=%s static_val=%s static_prem=%s nav_chg_pct=%s"
          % (r["code"], r["name"][:12], r.get("idx_code") if False else "",
             r.get("idx_price"), r.get("static_val"), r.get("static_prem"), r.get("nav_chg_pct")))

print("=" * 70)
print("ETF 行（结算汇兑四列复核）")
print("=" * 70)
et = board.build(market="A", limit=6)
shown = 0
for r in et["rows"]:
    if r.get("fx_mid") is not None:
        print("  %s %-12s iopv_prem=%s%% settle_prem=%s%% (fx_mid=%.4f settle_fx=%.4f @%s)"
              % (r["code"], r["name"][:12],
                 round(r["iopv_prem"], 2) if r["iopv_prem"] else None,
                 round(r["settle_prem"], 2) if r.get("settle_prem") is not None else None,
                 r["fx_mid"], r["settle_fx"], r["settle_date"]))
        shown += 1
    if shown >= 4:
        break

# 直接验证 static_history 弹窗数据（前端 /api/staticval 用同一函数）
print("=" * 70)
print("static_history 弹窗数据样例（160924 恒生指数LOF，HSI）")
print("=" * 70)
for row in _sv.static_history("160924", days=5, db=db):
    print("  %s nav=%.4f static=%.4f err=%+.2f%%" % (row["date"], row["nav"], row["static_val"], row["err_pct"]))
