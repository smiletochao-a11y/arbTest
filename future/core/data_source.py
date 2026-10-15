# -*- coding: utf-8 -*-
"""
数据源层：向顶层提供干净的 DataFrame。

两种后端（由 config.DATA_BACKEND 控制）：
  - "db"  : 读 SQLite（data/future.db）—— 默认
  - "csv" : 读 CSV 缓存（data/cache/）—— 回退用，保留完整原逻辑

本模块不负责抓取（抓取见 backend/fetch_data.py），只负责读取与清洗。

对外函数签名与返回**完全不变**，上层 engine.py / app.py 无需感知后端差异。
"""
import os
import re
import glob
from datetime import datetime, timedelta

import pandas as pd

from config import CACHE_DIR, VARIETIES, DATA_BACKEND
import db as _db


# ============ 基础读取 ============
def load_spot(variety):
    """读现货指数日线，返回 DataFrame(date, close)。"""
    cfg = VARIETIES.get(variety)
    if not cfg:
        return None
    code = cfg["spot_code"]

    if DATA_BACKEND == "db":
        with _db.cursor(readonly=True) as con:
            rows = con.execute(
                "SELECT date, close FROM index_history WHERE symbol=? ORDER BY date",
                (code,)).fetchall()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "spot_close"])
        # 与 CSV 后端保持列名一致：上层 engine 统一用 spot_close
        return df

    # ---- CSV 后端 ----
    f = os.path.join(CACHE_DIR, f"spot_{code}.csv")
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[["date", "close"]].rename(columns={"close": "spot_close"})


_FUTURES_CACHE = {}

def load_futures(symbol):
    """读单个合约日线，返回 DataFrame(date, close, volume, hold)。

    带进程内缓存：日线数据一天只变一次，缓存到进程重启为止安全。
    移仓助手/历史贴水会反复读同样的合约文件，不缓存时大量重复磁盘 IO。
    """
    if symbol in _FUTURES_CACHE:
        return _FUTURES_CACHE[symbol]

    if DATA_BACKEND == "db":
        with _db.cursor(readonly=True) as con:
            rows = con.execute(
                "SELECT date, close, volume, hold FROM futures_daily "
                "WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
        if not rows:
            _FUTURES_CACHE[symbol] = None
            return None
        out = pd.DataFrame(rows, columns=["date", "close", "volume", "hold"])
        _FUTURES_CACHE[symbol] = out
        return out

    # ---- CSV 后端 ----
    f = os.path.join(CACHE_DIR, f"fut_{symbol}.csv")
    if not os.path.exists(f):
        _FUTURES_CACHE[symbol] = None
        return None
    df = pd.read_csv(f)
    if not len(df):
        _FUTURES_CACHE[symbol] = None
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    cols = [c for c in ["date", "close", "volume", "hold"] if c in df.columns]
    out = df[cols]
    _FUTURES_CACHE[symbol] = out
    return out


_CONTRACT_CACHE = None

def load_contract_info():
    """读合约清单，返回 DataFrame（列名与 CSV 版保持一致，含中文列）。

    带内存缓存。上层 symbol_last_trade_date 依赖列名「合约代码」「最后交易日」，
    所以这里必须把英文列**翻回原中文列名**，否则上层会静默取不到值。
    """
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is not None:
        return _CONTRACT_CACHE

    if DATA_BACKEND == "db":
        with _db.cursor(readonly=True) as con:
            rows = con.execute(
                "SELECT symbol, contract_month, base_price, list_date, "
                "last_trade_date, limit_up_pct, limit_down_pct, limit_up_price, "
                "limit_down_price, hold_limit, variety, query_date FROM contract_master"
            ).fetchall()
        if not rows:
            return None
        # 列名与顺序**严格对齐 CSV 版**（品种列在 hold_limit 之后、query_date 之前）
        df = pd.DataFrame(rows, columns=[
            "合约代码", "合约月份", "挂盘基准价", "上市日", "最后交易日",
            "涨停板幅度", "跌停板幅度", "涨停板价位", "跌停板价位",
            "持仓限额", "品种", "查询交易日",
        ])
        _CONTRACT_CACHE = df
        return _CONTRACT_CACHE

    # ---- CSV 后端 ----
    f = os.path.join(CACHE_DIR, "contract_info.csv")
    if not os.path.exists(f):
        return None
    try:
        _CONTRACT_CACHE = pd.read_csv(f)
    except Exception:
        return None
    return _CONTRACT_CACHE


# ============ 合约代码工具 ============
def list_symbols(variety):
    """列出本地已有的某品种全部合约代码，按月份排序。

    ⚠️ SQLite 的 LIKE 对 ASCII **不区分大小写**，`LIKE 'i%'` 会连 IC/IF/IH/IM
    一起捞出来。所以这里改用 GLOB（区分大小写），大小写品种各归各的。
    """
    if DATA_BACKEND == "db":
        with _db.cursor(readonly=True) as con:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM futures_daily "
                "WHERE symbol GLOB ?", (f"{variety}[0-9][0-9][0-9][0-9]",)).fetchall()
        syms = [r[0] for r in rows]
        return sorted(syms, key=symbol_sort_key)

    # ---- CSV 后端 ----
    fs = glob.glob(os.path.join(CACHE_DIR, f"fut_{variety}*.csv"))
    syms = []
    for f in fs:
        name = os.path.basename(f)[4:-4]  # fut_IC2610.csv -> IC2610
        if re.match(rf"^{re.escape(variety)}\d{{4}}$", name):
            syms.append(name)
    return sorted(syms, key=symbol_sort_key)


def symbol_sort_key(sym):
    """IC2610 / i2609 -> (2610, 0)；用于按时间排序。

    品种代码**大小写都要认**（中金所大写 IC/IM/IF/IH，大商所小写 i）。
    """
    m = re.match(r"([A-Za-z]+)(\d{4})", sym)
    if not m:
        return (99999, 0)
    return (int(m.group(2)), 0)


def parse_symbol(sym):
    """IC2610 / i2609 -> dict(variety='IC', yy=26, mm=10, year=2026, month=10)"""
    m = re.match(r"([A-Za-z]+)(\d{2})(\d{2})", sym)
    if not m:
        return None
    v, yy, mm = m.group(1), int(m.group(2)), int(m.group(3))
    return {"variety": v, "yy": yy, "mm": mm, "year": 2000 + yy, "month": mm}


def _third_friday(year, month):
    """交割月第三个周五 —— 中金所股指期货的最后交易日。"""
    d = datetime(year, month, 1)
    fridays = []
    while d.month == month:
        if d.weekday() == 4:
            fridays.append(d)
        d += timedelta(days=1)
    return fridays[2] if len(fridays) >= 3 else None


def _nth_trading_day(year, month, n):
    """交割月第 n 个交易日（只跳周末）—— 大商所铁矿石是第 10 个交易日。"""
    d = datetime(year, month, 1)
    cnt = 0
    while d.month == month:
        if d.weekday() < 5:
            cnt += 1
            if cnt == n:
                return d
        d += timedelta(days=1)
    return None


def symbol_last_trade_date(sym):
    """
    取合约最后交易日。
    优先读官方合约清单（contract_master / contract_info.csv，最准确）；
    读不到时按**交易所规则**推算：
      · 中金所（IC/IM/IF/IH）→ 交割月第三个周五
      · 大商所（铁矿石 i）    → 交割月第 10 个交易日
    """
    # 1) 库/清单里的官方数据
    try:
        info = load_contract_info()
        if info is not None and "合约代码" in info.columns:
            r = info[info["合约代码"] == sym]
            if len(r) and pd.notna(r.iloc[0].get("最后交易日")):
                return str(r.iloc[0]["最后交易日"])[:10]
    except Exception:
        pass
    # 2) 规则推算
    p = parse_symbol(sym)
    if not p:
        return None
    ex = VARIETIES.get(p["variety"], {}).get("exchange", "cffex")
    if ex == "dce":
        d = _nth_trading_day(p["year"], p["month"], 10)
    else:
        d = _third_friday(p["year"], p["month"])
    return d.strftime("%Y-%m-%d") if d else None


def get_active_symbols(variety, as_of=None):
    """
    取某品种在某日"当时在交易的"合约。

    选取规则由品种配置决定（config.VARIETIES[variety]["active_mode"]）：
      · "near"（默认，股指 IC/IM/IF/IH）：按合约月份由近到远取前 active_count 个
        —— 即「当月 / 次月 / 当季 / 下季」。
      · "hold"（铁矿石）：按**持仓量**由大到小取前 active_count 个
        —— 铁矿石同时挂 12 个月份，但主力只在 01/05/09 之间轮换，
        「近月」常常是几乎没人交易的空壳，只有按持仓量才能选出真正的主力。
        见 docs/009 差异 1 / §6 第二步。

    as_of: 'YYYY-MM-DD'，默认取数据里最新日期。
    返回的列表**一律按合约月份升序**（近月在前）——
    engine.build_pairs 依赖这个顺序来生成「近月 − 远月」对。
    """
    cfg = VARIETIES.get(variety, {})
    mode = cfg.get("active_mode", "near")
    count = int(cfg.get("active_count", 4))

    syms = list_symbols(variety)
    if not syms:
        return []
    # 用最后交易日筛选：最后交易日 >= as_of 的合约才是当时在交易的
    if as_of is None:
        # 取所有合约里最新的日期
        latest = None
        for s in syms:
            df = load_futures(s)
            if df is not None and len(df):
                d = df["date"].iloc[-1]
                if latest is None or d > latest:
                    latest = d
        as_of = latest
    if as_of is None:
        return []
    alive = []
    for s in syms:
        ltd = symbol_last_trade_date(s)
        if ltd and ltd >= as_of:
            df = load_futures(s)
            if df is not None and len(df) and df["date"].iloc[0] <= as_of:
                alive.append(s)

    if mode == "hold":
        # 按「as_of 当日（或之前最近一天）的持仓量」降序排名
        ranked = []
        for s in alive:
            hold = 0.0
            df = load_futures(s)
            if df is not None and len(df) and "hold" in df.columns:
                sub = df[df["date"] <= as_of]
                if len(sub):
                    v = sub.iloc[-1]["hold"]
                    if pd.notna(v):
                        hold = float(v)
            ranked.append((s, hold))
        ranked.sort(key=lambda x: -x[1])
        picked = [s for s, _ in ranked[:count]]
        # 返回前按月份升序 —— build_pairs 需要近月在前
        return sorted(picked, key=symbol_sort_key)

    return sorted(alive, key=symbol_sort_key)[:count]

# ============ 基差计算 ============
def compute_basis_table(variety, as_of=None):
    """
    计算某品种当前基差看板数据。
    返回 list of dict:
      {symbol, price, spot, basis, basis_pct, annual, days_left, date}
    """
    syms = get_active_symbols(variety, as_of=as_of)
    spot = load_spot(variety)
    if spot is None or not syms:
        return []
    spot_map = dict(zip(spot["date"], spot["spot_close"]))
    out = []
    for s in syms:
        df = load_futures(s)
        if df is None or not len(df):
            continue
        last = df.iloc[-1]
        date = last["date"]
        price = float(last["close"])
        spot_close = spot_map.get(date)
        if spot_close is None:
            # 用最近的现货收盘价
            prev = spot[spot["date"] <= date]
            if not len(prev):
                continue
            spot_close = float(prev.iloc[-1]["spot_close"])
            date = prev.iloc[-1]["date"]
        basis = spot_close - price
        basis_pct = basis / spot_close * 100
        ltd = symbol_last_trade_date(s)
        days_left = None
        if ltd:
            days_left = (datetime.strptime(ltd, "%Y-%m-%d") - datetime.strptime(date, "%Y-%m-%d")).days
        annual = None
        if days_left and days_left > 0:
            annual = basis_pct * 365 / days_left
        out.append({
            "symbol": s,
            "price": round(price, 2),
            "spot": round(spot_close, 2),
            "basis": round(basis, 2),
            "basis_pct": round(basis_pct, 2),
            "annual": round(annual, 2) if annual is not None else None,
            "days_left": days_left,
            "date": date,
        })
    return out


def get_spot_series(variety):
    """返回现货收盘价时间序列 {date: close}。"""
    spot = load_spot(variety)
    if spot is None:
        return {}
    return dict(zip(spot["date"], spot["spot_close"]))


_LATEST_DATE_CACHE = None

def get_latest_date():
    """取全部数据里最新的交易日。带进程内缓存（日线一天只变一次）。"""
    global _LATEST_DATE_CACHE
    if _LATEST_DATE_CACHE is not None:
        return _LATEST_DATE_CACHE

    if DATA_BACKEND == "db":
        with _db.cursor(readonly=True) as con:
            r = con.execute("SELECT MAX(date) FROM futures_daily").fetchone()
        _LATEST_DATE_CACHE = r[0] if r else None
        return _LATEST_DATE_CACHE

    # ---- CSV 后端 ----
    latest = None
    for f in glob.glob(os.path.join(CACHE_DIR, "fut_*.csv")):
        try:
            df = pd.read_csv(f)
            if len(df):
                d = str(df["date"].iloc[-1])
                if latest is None or d > latest:
                    latest = d
        except Exception:
            continue
    _LATEST_DATE_CACHE = latest
    return latest


# ============ 缓存清理（供"页面上一键更新数据"使用） ============
def clear_caches():
    """清空本模块的全部进程内缓存。

    上面三个缓存都是「缓存到进程重启为止」的设计 —— 抓取新数据后
    必须调用本函数，否则服务会继续返回旧数据，页面看着像没更新。
    """
    global _CONTRACT_CACHE, _LATEST_DATE_CACHE
    _FUTURES_CACHE.clear()
    _CONTRACT_CACHE = None
    _LATEST_DATE_CACHE = None
