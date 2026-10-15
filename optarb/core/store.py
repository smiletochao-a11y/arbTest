# -*- coding: utf-8 -*-
"""
SQLite 存储（Optarb 移仓价差数据层）

风格对齐 future 项目的 arb_master.db：
  - 日期统一存 TEXT ISO（YYYY-MM-DD），便于排序与比较
  - INSERT OR REPLACE 幂等，可反复重跑
  - 带 updated_at

三张表：
  opt_daily    期权日行情（含 delta）
  fut_daily    期货日行情
  index_daily  标的指数日线
"""
import os
import sqlite3
from datetime import datetime

from core import config

DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS opt_daily (
    date TEXT NOT NULL,          -- YYYY-MM-DD
    symbol TEXT NOT NULL,        -- IO2610-P-4300
    variety TEXT NOT NULL,       -- IO / HO / MO
    expiry TEXT NOT NULL,        -- 2610
    cp TEXT NOT NULL,            -- C / P
    strike REAL NOT NULL,
    open REAL, high REAL, low REAL,
    close REAL, settle REAL, presettle REAL,
    volume INTEGER, amount REAL, oi INTEGER, oi_chg INTEGER,
    delta REAL,
    updated_at TEXT,
    PRIMARY KEY (date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_opt_variety_date ON opt_daily(variety, date);
CREATE INDEX IF NOT EXISTS idx_opt_expiry ON opt_daily(variety, expiry, date);

CREATE TABLE IF NOT EXISTS fut_daily (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    variety TEXT NOT NULL,
    expiry TEXT NOT NULL,
    open REAL, high REAL, low REAL,
    close REAL, settle REAL,
    volume INTEGER, amount REAL, oi INTEGER, oi_chg INTEGER,
    updated_at TEXT,
    PRIMARY KEY (date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_fut_variety_date ON fut_daily(variety, date);

CREATE TABLE IF NOT EXISTS index_daily (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,        -- sh000300
    close REAL,
    updated_at TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    date TEXT PRIMARY KEY,       -- YYYY-MM-DD
    ok INTEGER,
    n_opt INTEGER,
    n_fut INTEGER,
    updated_at TEXT
);

-- ETF 期权隐含波动率日序列（见 core/etf_iv.py / docs/009）
--   ★ atm_iv = (atm_call + atm_put) / 2 —— 页面唯一口径
--     （官方 IV 用 F=S·e^{rT} 无视分红，认购偏低/认沽偏高，取平均抵消到 ±0.9pp）
CREATE TABLE IF NOT EXISTS etf_iv_daily (
    date TEXT NOT NULL,          -- YYYY-MM-DD
    underlying TEXT NOT NULL,    -- 510050 / 510300 / 510500 / 588000 / 588080 / 159915
    expiry TEXT,                 -- 选中的最近月（2610）
    dte INTEGER,                 -- 该日到到期日剩余自然日
    strike REAL,                 -- 平值档行权价
    atm_call REAL,               -- 官方认购 ATM IV
    atm_put REAL,                -- 官方认沽 ATM IV（★ 不要单边使用）
    atm_iv REAL,                 -- ★ (atm_call+atm_put)/2
    p25_iv REAL,                 -- 25Δ 认沽 IV
    p10_iv REAL,                 -- 10Δ 认沽 IV
    src TEXT,                    -- sse（官方）/ szse（自算）
    updated_at TEXT,
    PRIMARY KEY (date, underlying)
);
CREATE INDEX IF NOT EXISTS idx_etfiv_und_date ON etf_iv_daily(underlying, date);
"""


def connect(path: str = None):
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 写入

def save_day(conn, date_iso: str, options, futures):
    """写入某一天的期权 + 期货（幂等）"""
    ts = _now()
    if options:
        conn.executemany(
            """INSERT OR REPLACE INTO opt_daily
               (date,symbol,variety,expiry,cp,strike,open,high,low,close,settle,
                presettle,volume,amount,oi,oi_chg,delta,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(date_iso, o["symbol"], o["variety"], o["expiry"], o["cp"], o["strike"],
              o["open"], o["high"], o["low"], o["close"], o["settle"], o["presettle"],
              o["volume"], o["amount"], o["oi"], o["oi_chg"], o.get("delta"), ts)
             for o in options])
    if futures:
        conn.executemany(
            """INSERT OR REPLACE INTO fut_daily
               (date,symbol,variety,expiry,open,high,low,close,settle,
                volume,amount,oi,oi_chg,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(date_iso, f["symbol"], f["variety"], f["expiry"],
              f["open"], f["high"], f["low"], f["close"], f["settle"],
              f["volume"], f["amount"], f["oi"], f["oi_chg"], ts)
             for f in futures])
    conn.commit()


def save_index(conn, symbol: str, rows):
    """rows = [{'day': '2026-09-24', 'close': 4466.0}]"""
    ts = _now()
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily (date,symbol,close,updated_at) VALUES (?,?,?,?)",
        [(r["day"], symbol, r["close"], ts) for r in rows])
    conn.commit()


def log_fetch(conn, date_iso: str, ok: bool, n_opt: int, n_fut: int):
    conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                 (date_iso, 1 if ok else 0, n_opt, n_fut, _now()))
    conn.commit()


# ---------------------------------------------------------------- 读取

def has_day(conn, date_iso: str) -> bool:
    r = conn.execute("SELECT ok FROM fetch_log WHERE date=?", (date_iso,)).fetchone()
    return bool(r and r["ok"])


def latest_date(conn, variety: str = None):
    if variety:
        r = conn.execute("SELECT MAX(date) d FROM opt_daily WHERE variety=?", (variety,)).fetchone()
    else:
        r = conn.execute("SELECT MAX(date) d FROM opt_daily").fetchone()
    return r["d"] if r and r["d"] else None


def load_options(conn, variety: str, date_iso: str, cp: str = None):
    """返回 [dict, ...]（sqlite3.Row -> dict）"""
    sql = "SELECT * FROM opt_daily WHERE variety=? AND date=?"
    args = [variety, date_iso]
    if cp:
        sql += " AND cp=?"
        args.append(cp)
    return [dict(r) for r in conn.execute(sql, args)]


def load_expiries(conn, variety: str, date_iso: str):
    """该日有行情的到期月（升序，如 ['2610','2611','2612','2703']）"""
    rows = conn.execute(
        "SELECT DISTINCT expiry FROM opt_daily WHERE variety=? AND date=? ORDER BY expiry",
        (variety, date_iso)).fetchall()
    return [r["expiry"] for r in rows]


def load_futures(conn, variety: str, date_iso: str):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fut_daily WHERE variety=? AND date=?", (variety, date_iso))]


def load_index_close(conn, symbol: str, date_iso: str):
    r = conn.execute("SELECT close FROM index_daily WHERE symbol=? AND date=?",
                     (symbol, date_iso)).fetchone()
    return r["close"] if r else None


def load_index_series(conn, symbol: str, start: str, end: str):
    return [dict(r) for r in conn.execute(
        "SELECT date, close FROM index_daily WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
        (symbol, start, end))]


def load_spread_series(conn, variety: str, strike: float, cp: str, start: str, end: str):
    """某一行权价在某段时间的收盘序列（用于月度走势）"""
    return [dict(r) for r in conn.execute(
        """SELECT date, expiry, close, settle FROM opt_daily
           WHERE variety=? AND strike=? AND cp=? AND date>=? AND date<=?
           ORDER BY date""", (variety, strike, cp, start, end))]


def stats(conn):
    o = conn.execute("SELECT COUNT(*) n, MIN(date) a, MAX(date) b FROM opt_daily").fetchone()
    f = conn.execute("SELECT COUNT(*) n FROM fut_daily").fetchone()
    return {"opt_rows": o["n"], "opt_from": o["a"], "opt_to": o["b"], "fut_rows": f["n"]}
