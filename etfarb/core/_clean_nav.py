"""临时：清理 fund_nav_history 2025 及以前数据（保留 2026 起）。删前备份。"""
import sqlite3, shutil, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import db as _db  # noqa: E402  复用 core/db.py 路径解析
DB = os.path.join(_db.DATA_DIR, "etfarb.db")
BAK = os.path.join(_db.DATA_DIR, "etfarb.db.bak-20260919-preclean")

shutil.copy2(DB, BAK)
print('backup ->', BAK, os.path.getsize(BAK), 'bytes')

c = sqlite3.connect(DB)
before = c.execute('SELECT COUNT(*) FROM fund_nav_history').fetchone()[0]
by_year = c.execute(
    "SELECT substr(date,1,4) y, COUNT(*) FROM fund_nav_history GROUP BY y ORDER BY y"
).fetchall()
c.execute("DELETE FROM fund_nav_history WHERE date < '2026-01-01'")
after = c.execute('SELECT COUNT(*) FROM fund_nav_history').fetchone()[0]
c.commit(); c.close()

print('LOCAL before=%d after=%d deleted=%d' % (before, after, before - after))
print('by_year_before=', by_year)
print('by_year_after =', c.execute(
    "SELECT substr(date,1,4) y, COUNT(*) FROM fund_nav_history GROUP BY y ORDER BY y"
).fetchall() if False else 'see above')
