# -*- coding: utf-8 -*-
"""诊断：A股/中证指数历史K线在东财 push2his 是否真的抓不到。只读，不改库。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import static_val as sv
from index_quote import clean_index_symbol, classify_index_symbol

CASES = ["399997", "000922.CSI", "930997.CSI", "证券公司", "000961.CSI", "399300"]
for code in CASES:
    cs = clean_index_symbol(code)
    kind, _ = classify_index_symbol(code)
    print("%-12s clean=%-12s kind=%s" % (code, cs, kind))

print("--- raw em kline probe ---")
for cs, secid in [("399997", "2.399997"), ("000922", "2.000922")]:
    try:
        url = sv._KLINE_URL % (secid, "20250101")
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        data = json.loads(raw).get("data") or {}
        kl = data.get("klines") or []
        print("secid=%s name=%s rows=%d first=%s last=%s" % (
            secid, data.get("name"), len(kl),
            (kl[0].split(",")[0] if kl else "-"), (kl[-1].split(",")[0] if kl else "-")))
        if not kl:
            print("  raw head:", raw[:300])
    except Exception as e:
        print("secid=%s EXC: %r" % (secid, e))

print("--- local db index_history counts ---")
import sqlite3
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "etfarb.db")
c = sqlite3.connect(DB)
for sym in ["399997", "000922", "399300", "930997"]:
    n = c.execute("SELECT COUNT(*) FROM index_history WHERE symbol=?", (sym,)).fetchone()[0]
    print("local index_history %s = %d rows" % (sym, n))
