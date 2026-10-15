# -*- coding: utf-8 -*-
"""
SQLite 数据层：替代原来的 CSV 缓存目录。

设计对齐东哥其他程序（参考 D:\\Study\\arbTest\\database\\arb_master.db）：
  - 表名英文小写下划线
  - 日期列 TEXT，ISO 格式 '2026-09-24'
  - 复合主键 (date, symbol)
  - 写入用 INSERT OR REPLACE，幂等可重跑
  - 连接开 WAL + busy_timeout，避免多进程抢锁

三张表：
  futures_daily    期货合约日线（date, symbol, open/high/low/close/volume/hold/settle）
  index_history    现货指数日线（symbol, date, close）
  contract_master  合约清单（symbol, 品种, 上市日, 最后交易日 ...）

为什么建这三张而不是原样照搬一份 CSV：CSV 的列名带中文（合约代码/最后交易日），
中文列名做 SQL 查询要反复加引号、易错，所以映射成英文列，业务代码里再翻回去。
"""
import os
import sqlite3
import sys
from contextlib import contextmanager

# 数据库路径：**唯一真源在 core/config.py**（本文件不再自行解析，避免两处漂移）。
# [AI-2026-10-07] 原写法 _ROOT=dirname(dirname(__file__)) 在本地会落到 future/src，
#   拼出 future/src/data/future.db（不存在）⇒ 只读打开直接
#   OperationalError: unable to open database file；而 config.DB_PATH 指向正确的
#   future/data/future.db。config.py 不 import 本模块，反向 import 无循环依赖。
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from config import DB_PATH  # noqa: E402


# ============ 建表 ============
SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_daily (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    hold REAL,
    settle REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS index_history (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS contract_master (
    symbol TEXT PRIMARY KEY,
    variety TEXT,
    contract_month TEXT,
    base_price REAL,
    list_date TEXT,
    last_trade_date TEXT,
    limit_up_pct REAL,
    limit_down_pct REAL,
    limit_up_price REAL,
    limit_down_price REAL,
    hold_limit REAL,
    query_date TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 查询优化：engine 每次请求都要按 symbol 拉整条序列
CREATE INDEX IF NOT EXISTS idx_futures_symbol_date ON futures_daily(symbol, date);
CREATE INDEX IF NOT EXISTS idx_index_symbol_date   ON index_history(symbol, date);
CREATE INDEX IF NOT EXISTS idx_contract_variety    ON contract_master(variety);
"""


def connect(readonly=False):
    """打开一个连接。

    readonly=True 时走 SQLite 的只读 URI，可避免误写、也不会因为并发写被阻塞。
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    else:
        con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA busy_timeout=30000")   # 多进程并发时等 30 秒再放弃
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")  # 读写不互斥
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextmanager
def cursor(readonly=False):
    """上下文管理器：自动 commit / close。"""
    con = connect(readonly=readonly)
    try:
        yield con
        if not readonly:
            con.commit()
    finally:
        con.close()


def init_db():
    """建表（幂等）。"""
    with cursor() as con:
        con.executescript(SCHEMA)


def db_exists():
    return os.path.exists(DB_PATH)


def table_count(table):
    """取某张表的行数，用于迁移校验。表不存在返回 -1。"""
    try:
        with cursor(readonly=True) as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return -1


# ============ 写入 ============
def upsert_futures(rows):
    """批量写期货日线。rows = [(date, symbol, open, high, low, close, volume, hold, settle), ...]"""
    if not rows:
        return 0
    with cursor() as con:
        con.executemany(
            "INSERT OR REPLACE INTO futures_daily "
            "(date, symbol, open, high, low, close, volume, hold, settle, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)",
            rows,
        )
    return len(rows)


def upsert_index(rows):
    """批量写指数日线。rows = [(symbol, date, close), ...]"""
    if not rows:
        return 0
    with cursor() as con:
        con.executemany(
            "INSERT OR REPLACE INTO index_history "
            "(symbol, date, close, updated_at) VALUES (?,?,?, CURRENT_TIMESTAMP)",
            rows,
        )
    return len(rows)


def upsert_contracts(rows):
    """批量写合约清单。rows = [(symbol, variety, month, base, list_date, ltd, ...), ...]"""
    if not rows:
        return 0
    with cursor() as con:
        con.executemany(
            "INSERT OR REPLACE INTO contract_master "
            "(symbol, variety, contract_month, base_price, list_date, last_trade_date, "
            " limit_up_pct, limit_down_pct, limit_up_price, limit_down_price, "
            " hold_limit, query_date, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)",
            rows,
        )
    return len(rows)


def clear_all():
    """清空三张表（仅迁移脚本用，慎调）。"""
    with cursor() as con:
        con.execute("DELETE FROM futures_daily")
        con.execute("DELETE FROM index_history")
        con.execute("DELETE FROM contract_master")


def stats():
    """返回各表行数，用于自检。"""
    out = {}
    with cursor(readonly=True) as con:
        for t in ("futures_daily", "index_history", "contract_master"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = -1
        # 合约数 & 日期范围
        try:
            out["futures_symbols"] = con.execute(
                "SELECT COUNT(DISTINCT symbol) FROM futures_daily").fetchone()[0]
            r = con.execute("SELECT MIN(date), MAX(date) FROM futures_daily").fetchone()
            out["futures_date_min"], out["futures_date_max"] = r[0], r[1]
            out["index_symbols"] = con.execute(
                "SELECT COUNT(DISTINCT symbol) FROM index_history").fetchone()[0]
        except sqlite3.Error:
            pass
    return out
