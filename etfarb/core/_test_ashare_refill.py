# -*- coding: utf-8 -*-
"""本地实测：A股指数改腾讯后，LOF 静态估值能否出数。清 idx_hist TTL 标记 → 重抓 → 验证。"""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import static_val as sv

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "etfarb.db")
c = sqlite3.connect(DB)
n = c.execute("DELETE FROM fetch_state WHERE src='idx_hist'").rowcount
c.commit()
print("cleared idx_hist TTL marks:", n)

# 本地 LOF 池的 idx_codes（A 股类样例）
cases = ["399997", "000922.CSI", "证券公司", "399300", "930914.CSI", "HSSI"]
res = sv.ensure_index_history(cases, db=DB)
for k, v in res.items():
    print("ensure %-12s -> %s rows" % (k, v))

print("--- static_history after refill ---")
for code, name in [("161725", "招商中证白酒"), ("501227", "泓德红利优选"), ("161631", "人工智能")]:
    rows = sv.static_history(code, days=5, db=DB)
    print("%s %s: %d rows" % (code, name, len(rows)))
    for r in rows[:3]:
        print("   ", r)
