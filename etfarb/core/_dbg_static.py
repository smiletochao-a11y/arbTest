# -*- coding: utf-8 -*-
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3
import etf_iopv_quote as eiq
import static_val as _sv

db = eiq.DB_PATH
code = "160322"
c = sqlite3.connect(db)
r = c.execute("SELECT fund_code, fund_name, idx_code, val_method FROM fund_profile WHERE fund_code=?", (code,)).fetchone()
c.close()
ic = r[2]
from index_quote import clean_index_symbol
sym = clean_index_symbol(ic)
print("idx_code=%s clean=%s" % (ic, sym))

c = sqlite3.connect(db)
navn = c.execute("SELECT COUNT(*) FROM fund_nav_history WHERE fund_code=?", (code,)).fetchone()[0]
ihc = c.execute("SELECT COUNT(*) FROM index_history WHERE symbol=?", (sym,)).fetchone()[0]
print("fund_nav_history count=%d  index_history(%s) count=%d" % (navn, sym, ihc))
rows = c.execute("SELECT date, close FROM index_history WHERE symbol=? ORDER BY date DESC LIMIT 3", (sym,)).fetchall()
print("recent index_history:", rows)
c.close()

print("ensure_index_history(HSCI) -> rows:", _sv.ensure_index_history([ic], db))
print("fetch_nav_history(160322) -> rows:", _sv.fetch_nav_history(code, db))
print("static_history:", _sv.static_history(code, days=5, db=db))
