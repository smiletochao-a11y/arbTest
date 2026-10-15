# -*- coding: utf-8 -*-
"""
计算引擎：历史基差序列、分位数、移仓价差分析。

口径说明（务必与 config.py 保持一致）：
  基差(basis) = 现货收盘价 - 期货收盘价
      > 0 → 贴水（期货比现货便宜），这是 IC/IM 的常态
  基差年化 = (基差 / 现货价) * (365 / 剩余天数) * 100%

  价差(spread) = 远月价格 - 近月价格
      < 0 → 远月更便宜（正常贴水结构）
  价差年化 = (价差 / 近月价格) * (365 / 两合约月份差对应天数) * 100%
"""
import os
from datetime import datetime

import pandas as pd

from config import CACHE_DIR, VARIETIES
from data_source import (
    load_spot, load_futures, list_symbols, parse_symbol,
    symbol_last_trade_date, symbol_sort_key,
)


# ============ 分位数 ============
def percentile_of(series, value):
    """value 在 series 中的百分位（0~100）。"""
    s = pd.Series(series).dropna()
    if not len(s) or value is None:
        return None
    return float((s < value).sum()) / len(s) * 100


def quantiles(series, qs=(0.3, 0.5, 0.75)):
    s = pd.Series(series).dropna()
    if not len(s):
        return {q: None for q in qs}
    return {q: float(s.quantile(q)) for q in qs}


def metric_stats(series, metric, current=None, qs=(0.25, 0.5, 0.75)):
    """
    对一组序列（每个元素含 annual / basis / spot）按指定指标算统计值。

    metric: 'annual' | 'basis' | 'spot'
    current: 当前值（可传实时值）；不传则用序列最后一个非空值

    返回 {current, percentile, q25, q50, q75, sample, start, end}
    """
    if not series:
        return None
    key = metric if metric in ("annual", "basis", "spot") else "annual"
    vals, dates = [], []
    for r in series:
        v = r.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if pd.isna(v):
            continue
        vals.append(v)
        dates.append(r.get("date"))
    if not vals:
        return None
    s = pd.Series(vals)
    cur = float(current) if current is not None else float(vals[-1])
    pct = float((s < cur).sum()) / len(s) * 100
    q = {k: float(s.quantile(k)) for k in qs}
    return {
        "current": round(cur, 2),
        "percentile": round(pct, 1),
        "q25": round(q[qs[0]], 2),
        "q50": round(q[qs[1]], 2),
        "q75": round(q[qs[2]], 2),
        "sample": len(s),
        "start": dates[0],
        "end": dates[-1],
    }


# ============ 现货指数点位分位 ============
def spot_percentile(variety, years=3, current_value=None):
    """
    当前现货指数点位在近 N 年历史中的分位（0~100）。
    current_value: 可传入实时点位；不传则用最新收盘价。

    返回: {percentile, current, sample, start, end, level, level_text}
      level: high / mid / low  → 用于前端打标签
    """
    spot = load_spot(variety)
    if spot is None or not len(spot):
        return None
    cutoff = (pd.Timestamp.now() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    sub = spot[spot["date"] >= cutoff]
    if len(sub) < 20:
        sub = spot
    cur = float(current_value) if current_value is not None else float(sub.iloc[-1]["spot_close"])
    pct = float((sub["spot_close"] < cur).sum()) / len(sub) * 100
    # 分档：>70% 偏高，30%~70% 中位，<30% 偏低
    if pct >= 70:
        level, txt = "high", "历史偏高"
    elif pct <= 30:
        level, txt = "low", "历史偏低"
    else:
        level, txt = "mid", "历史中位"
    return {
        "percentile": round(pct, 1),
        "current": round(cur, 2),
        "sample": int(len(sub)),
        "start": sub["date"].iloc[0],
        "end": sub["date"].iloc[-1],
        "years": years,
        "level": level,
        "level_text": txt,
    }


# ============ 1. 单合约基差时间序列 ============
def basis_series(variety, symbol):
    """
    返回某合约的基差时间序列 DataFrame(date, fut, spot, basis, basis_pct, annual, days_left)。
    """
    fut = load_futures(symbol)
    spot = load_spot(variety)
    if fut is None or spot is None or not len(fut):
        return None
    spot_map = dict(zip(spot["date"], spot["spot_close"]))
    ltd = symbol_last_trade_date(symbol)
    rows = []
    for _, r in fut.iterrows():
        d = r["date"]
        sp = spot_map.get(d)
        if sp is None:
            continue
        price = float(r["close"])
        basis = sp - price
        days_left = None
        if ltd:
            days_left = (datetime.strptime(ltd, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
        annual = None
        if days_left and days_left > 0:
            annual = (basis / sp) * 365 / days_left * 100
        rows.append({
            "date": d, "fut": price, "spot": sp,
            "basis": round(basis, 2),
            "basis_pct": round(basis / sp * 100, 3),
            "annual": round(annual, 2) if annual is not None else None,
            "days_left": days_left,
        })
    df = pd.DataFrame(rows)
    return df if len(df) else None


# ============ 2. 品种历史基差（用"次月合约"作基准，对齐参考程序） ============
# 注意：本函数要遍历品种下全部历史合约，单次约 5~8 秒。
# 结果按品种缓存（进程内），避免每次请求都重算 —— 日线数据一天只更新一次，
# 缓存到进程重启为止是安全的。
_VARIETY_HISTORY_CACHE = {}


def variety_history(variety, use_cache=True):
    """
    构建品种的历史基差/年化序列。
    做法：对每个交易日，取当时在交易的"次月合约"（第2近月）作为基准，
    计算该合约当日的基差与年化。

    结果会缓存在进程内存里（日线级数据，一天变一次）。
    """
    if use_cache and variety in _VARIETY_HISTORY_CACHE:
        return _VARIETY_HISTORY_CACHE[variety]
    syms = list_symbols(variety)
    spot = load_spot(variety)
    if not syms or spot is None:
        return None
    spot_map = dict(zip(spot["date"], spot["spot_close"]))
    all_dates = sorted(spot_map.keys())

    # 预载每个合约的数据
    fut_data = {}
    for s in syms:
        df = load_futures(s)
        if df is not None and len(df):
            fut_data[s] = df.set_index("date")

    rows = []
    for d in all_dates:
        # 找出当日可交易的合约
        alive = []
        for s, df in fut_data.items():
            if d in df.index:
                ltd = symbol_last_trade_date(s)
                if ltd and ltd >= d:
                    alive.append(s)
        if not alive:
            continue
        alive = sorted(alive, key=symbol_sort_key)
        if len(alive) < 2:
            continue
        # 次月合约 = 第 2 个
        near = alive[0]
        second = alive[1]
        price = float(fut_data[second].loc[d]["close"])
        sp = spot_map[d]
        basis = sp - price
        ltd = symbol_last_trade_date(second)
        days_left = (datetime.strptime(ltd, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
        annual = None
        if days_left > 0:
            annual = (basis / sp) * 365 / days_left * 100
        rows.append({
            "date": d,
            "symbol": second,
            "fut": round(price, 2),
            "spot": round(sp, 2),
            "basis": round(basis, 2),
            "annual": round(annual, 2) if annual is not None else None,
        })
    df = pd.DataFrame(rows)
    out = df if len(df) else None
    if use_cache:
        _VARIETY_HISTORY_CACHE[variety] = out
    return out


# ============ 3. 移仓价差（两两合约对） ============
# 进程内缓存：日线数据一天只变一次，缓存到重启为止安全。
# 不加缓存时，移仓助手（4 品种 × 6 合约对）要反复遍历近百个历史 CSV，
# 并发请求下因 GIL 互相阻塞，整体响应可达 40 秒以上。
_PAIR_SERIES_CACHE = {}
_SPREAD_POOL_CACHE = {}


def build_pairs(variety, as_of=None, symbol_list=None):
    """
    构造合约两两组合（近月在前），返回 [(near, far), ...]。
    symbol_list 默认取最近 4 个在交易合约。
    """
    from data_source import get_active_symbols
    syms = symbol_list or get_active_symbols(variety, as_of=as_of)
    pairs = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            pairs.append((syms[i], syms[j]))
    return pairs


def pair_spread_series(near, far):
    """
    计算一对合约的价差时间序列。

    业界通用约定（Wind / 同花顺 / 集思录一致）：
      价差 = 近月收盘 - 远月收盘
      正数 → 远月更便宜（贴水结构），移仓可获贴水收益
      负数 → 远月更贵（升水结构），移仓需付出成本

    与「基差 = 现货 - 期货，正数表示贴水」保持同向：正数都是贴水。

    返回 DataFrame(date, near, far, spread, annual)
    """
    ck = (near, far)
    if ck in _PAIR_SERIES_CACHE:
        return _PAIR_SERIES_CACHE[ck]
    dn = load_futures(near)
    df_ = load_futures(far)
    if dn is None or df_ is None:
        _PAIR_SERIES_CACHE[ck] = None
        return None
    m_near = dict(zip(dn["date"], dn["close"]))
    m_far = dict(zip(df_["date"], df_["close"]))
    common = sorted(set(m_near.keys()) & set(m_far.keys()))
    if not common:
        return None
    # 月份差：用合约月份差折算（跨几个月）
    pn, pf = parse_symbol(near), parse_symbol(far)
    month_gap = (pf["year"] * 12 + pf["month"]) - (pn["year"] * 12 + pn["month"])
    rows = []
    for d in common:
        n = float(m_near[d])
        f = float(m_far[d])
        spread = n - f   # 近月 - 远月（业界约定）
        # 年化：按 (价差/近月) * (12/月份差) * 100
        annual = None
        if month_gap > 0 and n:
            annual = (spread / n) * (12 / month_gap) * 100
        rows.append({
            "date": d, "near": round(n, 2), "far": round(f, 2),
            "spread": round(spread, 2),
            "annual": round(annual, 2) if annual is not None else None,
        })
    df = pd.DataFrame(rows)
    if len(df):
        df.attrs["month_gap"] = month_gap
    _PAIR_SERIES_CACHE[ck] = df if len(df) else None
    return df if len(df) else None


def spread_pool(variety, month_gap, years=1):
    """
    构建「同类合约对」的历史价差池（口径 B）。

    做法：找出历史上所有月份跨度 = month_gap 的相邻合约对（如跨1月：
    2501-2502、2502-2503、…、2610-2611），把它们各自的价差序列
    合并成一个池子，作为分位基准。

    这样做的理由：单个合约对（如 2610-2611）的生命周期很短
    （新合约刚挂牌时可能只有几天数据），不足以算分位；
    合并同类合约对可获得充足样本（近1年约 312 天）。

    注意：代价是混合了不同年份、不同剩余期限的合约，含轻微失真。

    结果按 (品种, 跨度, 年数) 缓存：日线数据一天只变一次，缓存到进程重启为止安全。
    缓存很关键 —— 移仓助手一次要算 6 个合约对，每对都要遍历近百个历史合约，
    不缓存时 4 个品种并发能把响应拖到 40 秒以上（GIL 下 pandas 互相阻塞）。
    """
    ck = (variety, month_gap, years)
    if ck in _SPREAD_POOL_CACHE:
        return _SPREAD_POOL_CACHE[ck]

    syms = list_symbols(variety)
    if not syms:
        return None
    # 找所有月份跨度匹配的合约对。
    #
    # 注意：这里必须穷举「任意两合约」组合，不能用「序号相邻」。
    # 原因：中金所只挂当月/次月/当季/下季 4 个月份，但数据商保留了历史上
    # 连续月份的合约文件（IC2601、IC2602、IC2604… 都有）。若只取序号相邻，
    # 相邻月份差永远只有 1 或 3，**永远配不出跨2月的对**（gap=2），
    # 导致「跨2月」退化为只剩当前那一对自身序列（约 20 余天，分位无意义）。
    # 穷举全组合后，gap=1/2/3 都能凑齐（如 IC2610-IC2612 属 gap=2）。
    pairs = []
    for i in range(len(syms)):
        pa = parse_symbol(syms[i])
        if not pa:
            continue
        for j in range(i + 1, len(syms)):
            pb = parse_symbol(syms[j])
            if not pb:
                continue
            gap = (pb["year"] * 12 + pb["month"]) - (pa["year"] * 12 + pa["month"])
            if gap == month_gap:
                pairs.append((syms[i], syms[j]))
    if not pairs:
        _SPREAD_POOL_CACHE[ck] = None
        return None
    cutoff = None
    if years:
        cutoff = (pd.Timestamp.now() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    rows = []
    for a, b in pairs:
        df = pair_spread_series(a, b)
        if df is None or not len(df):
            continue
        sub = df[df["date"] >= cutoff] if cutoff else df
        for _, r in sub.iterrows():
            rows.append({"date": r["date"], "spread": r["spread"],
                         "annual": r["annual"], "pair": f"{a}-{b}"})
    if not rows:
        _SPREAD_POOL_CACHE[ck] = None
        return None
    out = pd.DataFrame(rows)
    out.attrs["pair_count"] = len(pairs)
    _SPREAD_POOL_CACHE[ck] = out
    return out


def pair_analysis(near, far, years=1, live_spread=None, live_annual=None, live_date=None):
    """
    对一对合约做完整分析：当前价差、年化、历史分位、分位参照。

    分位基准采用**口径 B**（同类合约对合并池），与业界程序对齐。
    live_spread / live_annual: 可传入实时值，用于计算"当前"分位。
    live_date: 实时数据的日期（传了说明当前价是实时价）。
    """
    pn, pf = parse_symbol(near), parse_symbol(far)
    if not pn or not pf:
        return None
    month_gap = (pf["year"] * 12 + pf["month"]) - (pn["year"] * 12 + pn["month"])

    # 当前值：优先用实时
    cur_spread = live_spread
    cur_annual = live_annual
    cur_date = live_date
    df_self = pair_spread_series(near, far)
    if df_self is not None and len(df_self):
        last = df_self.iloc[-1]
        if cur_date is None:
            cur_date = last["date"]
        if cur_spread is None:
            cur_spread = float(last["spread"])
        if cur_annual is None and pd.notna(last["annual"]):
            cur_annual = float(last["annual"])
    if cur_spread is None:
        return None

    # 分位基准：优先用同类合约对合并池（口径 B）
    pool = spread_pool_by_variety(near, month_gap, years=years)
    basis_src = "merged"   # merged=合并同类合约对 / self=自身序列
    if pool is None or len(pool) < 50:
        # 样本太少，退化为自身序列
        pool = df_self
        basis_src = "self"
    if pool is None or not len(pool):
        return None

    pct = percentile_of(pool["spread"], cur_spread)
    q = quantiles(pool["spread"], (0.3, 0.5, 0.75))
    mean_v = float(pool["spread"].mean())

    # 曲线数据：必须用「合并池」而不是「本对自身序列」。
    # 原因：IC2610-IC2611 这类新合约对挂牌才几天，自身序列只有 3~5 个点，
    # 画出来是一条几乎看不出走势的短线；而分位线用的是合并池（313 天），
    # 两者不匹配会导致"背景曲线极短、横线悬在空中"的错觉。
    # 改用合并池后，曲线与分位线来自同一套样本，与参考程序一致。
    if basis_src == "merged" and "pair" in pool.columns:
        ser_df = pool[["date", "spread"]].sort_values("date")
    else:
        ser_df = df_self[["date", "spread"]] if df_self is not None else None
    series_out = ([{"date": r["date"], "spread": r["spread"]}
                   for _, r in ser_df.iterrows()] if ser_df is not None and len(ser_df) else [])

    return {
        "near": near, "far": far,
        "month_gap": month_gap,
        "date": cur_date,
        "spread": round(cur_spread, 2),
        "annual": round(cur_annual, 2) if cur_annual is not None else None,
        "percentile": round(pct, 1) if pct is not None else None,
        "sample": int(len(pool)),
        "basis_src": basis_src,
        "q30": round(q[0.3], 2) if q[0.3] is not None else None,
        "q50": round(q[0.5], 2) if q[0.5] is not None else None,
        "q75": round(q[0.75], 2) if q[0.75] is not None else None,
        "mean": round(mean_v, 2),
        "series": series_out,
        "series_src": basis_src,
    }


def spread_pool_by_variety(symbol, month_gap, years=1):
    """从合约代码解析出品种，再取该品种的同类合约对价差池。"""
    p = parse_symbol(symbol)
    if not p:
        return None
    return spread_pool(p["variety"], month_gap, years=years)


def move_helper_table(variety, years=1, as_of=None, live_prices=None, live_date=None):
    """
    移仓助手主表：所有合约对的当前分析。
    live_prices: {symbol: price} 实时价，用于覆盖当前价差。
    live_date: 实时数据日期，透传给 pair_analysis 标记当前价来源。

    年化公式： (价差 / 近月价) * (12 / 月份差) * 100
      - 近月价优先用实时价，无实时价则用最近收盘价
    """
    pairs = build_pairs(variety, as_of=as_of)
    out = []
    for near, far in pairs:
        ls = la = None
        if live_prices and near in live_prices and far in live_prices:
            ls = live_prices[near] - live_prices[far]
            pn = parse_symbol(near)
            pf = parse_symbol(far)
            gap = (pf["year"] * 12 + pf["month"]) - (pn["year"] * 12 + pn["month"])
            if gap > 0 and live_prices[near]:
                la = ls / live_prices[near] * (12 / gap) * 100
        a = pair_analysis(near, far, years=years, live_spread=ls,
                          live_annual=la, live_date=live_date)
        if a:
            out.append(a)
    return out


# ============ 缓存清理（供"页面上一键更新数据"使用） ============
def clear_caches():
    """清空本模块的三个进程内缓存。

    与 data_source.clear_caches() 配套：抓取新数据后两个模块都要清，
    否则"历史贴水"和"移仓助手"两页仍会显示旧数据。
    """
    _VARIETY_HISTORY_CACHE.clear()
    _PAIR_SERIES_CACHE.clear()
    _SPREAD_POOL_CACHE.clear()
