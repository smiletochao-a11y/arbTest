# -*- coding: utf-8 -*-
"""
交割日窗口（T-1 ~ T-10）移仓价差分布

复刻参考程序的《中证1000 股指期权 · 平值卖沽「当月 → 次月」移仓价差》月报。
（算法还原见 docs/004；对账脚本 scripts/verify_window.py）

口径（从截图逐条读出 + 实测验证）：
  · 横轴 = 过去 N 次月度到期
  · 每个点 = 该月「距交割 T-N 个**应开市交易日**」当天的 当月→次月 移仓价差
  · 档位   = 当月**平值**行权价（可切 上一档 K+1 / 下一档 K−1）
  · 价差   = 次月P(K)收盘 − 当月P(K)收盘（零成交用结算价，见 migrate._px）
  · 到期日遇法定假日**顺延**（见 migrate.effective_expiry）

★ 2026-09-27 实测对账：MO 卖沽 12 个月 T-1，**11/12 与参考程序逐位吻合**
  （140.2/167.8/182.4/200.2/203.4/207.0/220.0/238.8/242.0/255.6/259.0 vs
    140  /169  /182  /200  /203  /207  /218  /239  /242  /256  /259）
  指标卡：均值 211.17（他 211.25）、中位 218.5（他 218.60）。
  唯一不同 = 26/02：他用「补值」取了**到期日当天**的值（219），
  我们用**节前最后一个交易日**的值（172）—— 后者才是真能下单的日子，见 docs/004 §4。
"""
from collections import OrderedDict
from datetime import date, datetime, timedelta

from core import config, migrate, store


# ------------------------------------------------------------------ 基础

def trade_days(conn, variety: str):
    """该品种有行情的交易日（升序 ISO）"""
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM opt_daily WHERE variety=? ORDER BY date", (variety,))]


def _ym(code: str):
    return (2000 + int(code[:2]), int(code[2:]))


def _next_ym(code: str):
    y, m = _ym(code)
    return (y + (1 if m == 12 else 0), 1 if m == 12 else m + 1)


def _prev_ym_code(code: str):
    y, m = _ym(code)
    return f"{(y - 1) % 100:02d}12" if m == 1 else f"{y % 100:02d}{m - 1:02d}"


# ---- 日级缓存：build_window 要反复读同一天的期权，不缓存会很慢 ----
_OPT_CACHE = {}


def _opts(conn, variety: str, day: str, cp: str):
    key = (variety, day, cp)
    v = _OPT_CACHE.get(key)
    if v is None:
        v = store.load_options(conn, variety, day, cp)
        if len(_OPT_CACHE) > 4096:
            _OPT_CACHE.clear()
        _OPT_CACHE[key] = v
    return v


def spot_on(conn, variety: str, day: str):
    sym = config.VARIETIES.get(variety, {}).get("index")
    return store.load_index_close(conn, sym, day) if sym else None


# ------------------------------------------------------------------ 月度窗口

def windows(conn, variety: str, months: int = 12):
    """
    可用的「月度到期」列表（最近的 months 个），升序。

    入选条件（少一条都会算错）：
      ① 当月与次月**相邻**
      ② 当月**实际到期日**落在库内数据范围内
         —— 否则 T-N 会退化成"数据末尾"，系统性低估（future 项目踩过的坑）
    """
    days = trade_days(conn, variety)
    day_set = set(days)
    exps = [r["expiry"] for r in conn.execute(
        "SELECT DISTINCT expiry FROM opt_daily WHERE variety=? ORDER BY expiry", (variety,))]
    out = []
    for i in range(len(exps) - 1):
        e, f = exps[i], exps[i + 1]
        if _next_ym(e) != _ym(f):
            continue
        exp_d = migrate.effective_expiry(conn, variety, e)
        if exp_d not in day_set:
            continue
        out.append({"near": e, "far": f, "expiry": exp_d})
    return out[-months:] if months else out


def workday_before(d_iso: str, n: int):
    """
    参考程序的「应开市日历」：到期日前第 n 个**工作日**
    —— 只排周末，**不排法定假日**。用于判断某个月份的 T-N 是否落在长假里。
    """
    d = date.fromisoformat(d_iso)
    c = 0
    while c < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            c += 1
    return d.isoformat()


def t_minus(conn, variety: str, expiry_code: str, n: int, days=None):
    """
    距交割 T-N 个应开市交易日的日期。T-1 = 到期日的**前一个**交易日。

    ★ 约束：该日期必须**晚于上一个月合约的到期日** —— 否则当月合约还不是当月，
      「M → M+1 移仓」这个动作根本不成立（M 那时还是次月）。
      这条约束让 T-N 可以安全扩到 T-20 左右（合约通常提前约一个月成为当月）。
    """
    days = days or trade_days(conn, variety)
    exp_d = migrate.effective_expiry(conn, variety, expiry_code)
    prev_exp = migrate.effective_expiry(conn, variety, _prev_ym_code(expiry_code))
    seq = [d for d in days if d <= exp_d and (not prev_exp or d > prev_exp)]
    if len(seq) <= n:
        return None
    return seq[-1 - n]


# ------------------------------------------------------------------ 单点价差

def spread_at(conn, variety: str, near: str, far: str, day: str,
              cp: str = "P", offset: int = 0, want_iv: bool = False):
    """
    某日：当月 near → 次月 far，取**当月平值档**（offset=0）
    或 上一档（+1）/ 下一档（−1）。

    offset 的定义与参考程序一致：**按行权价升序，+1 = 更高一档**。
    """
    if not day:
        return None
    spot = spot_on(conn, variety, day)
    if not spot:
        return None
    rows = _opts(conn, variety, day, cp)
    near_by_k = {o["strike"]: o for o in rows if o["expiry"] == near}
    far_by_k = {o["strike"]: o for o in rows if o["expiry"] == far}
    ks = sorted(set(near_by_k) & set(far_by_k))
    if not ks:
        return None
    atm = min(ks, key=lambda k: abs(k - spot))
    i = ks.index(atm) + offset
    if i < 0 or i >= len(ks):
        return None
    k = ks[i]
    n, f = near_by_k[k], far_by_k[k]
    pn, pf = migrate._px(n), migrate._px(f)
    if pn is None or pf is None:
        return None

    cell = {
        "date": day, "strike": k, "atm": atm, "spot": spot,
        "buyback": pn, "sell": pf, "net": round(pf - pn, 2),
        "net_pct": round((pf - pn) / spot * 100, 4) if spot else None,
        "zero_vol": (n.get("volume") or 0) == 0 or (f.get("volume") or 0) == 0,
    }
    if want_iv:
        # IV 的远期取现货价（对齐参考程序，见 docs/002 §2.1）
        _, dte_n = migrate.years_to_expiry(day, near, migrate.effective_expiry(conn, variety, near))
        _, dte_f = migrate.years_to_expiry(day, far, migrate.effective_expiry(conn, variety, far))
        T_n, T_f = dte_n / 365.0, dte_f / 365.0
        iv_n = migrate.implied_vol(pn, spot, k, T_n, migrate.R_DEFAULT, cp)
        iv_f = migrate.implied_vol(pf, spot, k, T_f, migrate.R_DEFAULT, cp)
        cell["dte_near"], cell["dte_far"] = dte_n, dte_f
        cell["iv_near"] = round(iv_n * 100, 2) if iv_n else None
        cell["iv_far"] = round(iv_f * 100, 2) if iv_f else None
    return cell


# ------------------------------------------------------------------ 统计

def _stats(vals, spots, iv_n=None, iv_f=None):
    if not vals:
        return None
    vs = sorted(vals)
    k = len(vs)
    med = vs[k // 2] if k % 2 else (vs[k // 2 - 1] + vs[k // 2]) / 2

    def _q(p):
        if k == 1:
            return vs[0]
        pos = p * (k - 1)
        lo = int(pos)
        hi = min(lo + 1, k - 1)
        return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)

    out = {
        "sample": k,
        "mean": round(sum(vals) / k, 2),
        "median": round(med, 2),
        "p25": round(_q(0.25), 2),
        "p75": round(_q(0.75), 2),
        "min": round(vs[0], 2),
        "max": round(vs[-1], 2),
    }
    if spots:
        out["ratio_pct"] = round(sum(v / s for v, s in zip(vals, spots)) / len(spots) * 100, 3)
    if iv_n:
        out["iv_near"] = round(sum(iv_n) / len(iv_n), 2)
    if iv_f:
        out["iv_far"] = round(sum(iv_f) / len(iv_f), 2)
    return out


def percentile_of(vals, x):
    """x 在 vals 里的分位（0~100），越大表示价差越厚"""
    if not vals or x is None:
        return None
    below = sum(1 for v in vals if v < x)
    equal = sum(1 for v in vals if v == x)
    return round((below + 0.5 * equal) / len(vals) * 100, 1)


# ------------------------------------------------------------------ 主入口

def build_window(conn, variety: str, cp: str = "P", months: int = 12,
                 offset: int = 0, tmax: int = 10):
    """
    生成完整的「T-1 ~ T-tmax × 过去 months 个月度到期」矩阵。

    返回 dict：
      variety/cp/offset/months/tmax
      series: [{near, far, expiry, cells:[cell|null × tmax]}]
      stats:  {"1": {...}, ..., "10": {...}}   # 每个 T-N 的横截面统计
      atm_range: [min, max]                     # 各月平值档的行权价区间
      all_nets: {n: [每月价差]}                 # 供前端算分位
    """
    wins = windows(conn, variety, months)
    if not wins:
        return {"ok": False, "error": f"{variety} 没有可用的月度到期（库内数据不足）"}
    days = trade_days(conn, variety)

    series, all_nets, all_spots, all_ivn, all_ivf, atm_ks = [], {}, {}, {}, {}, []
    day_set = set(days)
    for w in wins:
        cells = []
        for n in range(1, tmax + 1):
            d = t_minus(conn, variety, w["near"], n, days)
            c = spread_at(conn, variety, w["near"], w["far"], d, cp, offset,
                          want_iv=True) if d else None
            if c:
                c["t"] = n
                # 参考程序的「补值」标记：该月 T-N 的**工作日历目标日**落在休市日，
                # 他就必须补值（图上打星号、柱画成浅色）。我们不用补值，见 docs/004 §3。
                c["holiday"] = workday_before(w["expiry"], n) not in day_set
                all_nets.setdefault(n, []).append(c["net"])
                all_spots.setdefault(n, []).append(c["spot"])
                if c.get("iv_near") is not None:
                    all_ivn.setdefault(n, []).append(c["iv_near"])
                if c.get("iv_far") is not None:
                    all_ivf.setdefault(n, []).append(c["iv_far"])
                if n == 1:
                    atm_ks.append(c["strike"])
            cells.append(c)
        series.append({**w, "cells": cells})

    stats = {}
    for n in range(1, tmax + 1):
        s = _stats(all_nets.get(n, []), all_spots.get(n, []),
                   all_ivn.get(n), all_ivf.get(n))
        if s:
            stats[str(n)] = s

    return {
        "ok": True,
        "variety": variety, "cp": cp, "offset": offset,
        "months": len(series), "tmax": tmax,
        "series": series,
        "stats": stats,
        "atm_range": [min(atm_ks), max(atm_ks)] if atm_ks else None,
        "all_nets": {str(k): v for k, v in all_nets.items()},
    }


# ------------------------------------------------------------------ 缓存
#
# ★ 2026-09-27：实时表「历史分位」列改为**每档用各自 offset 的分布**后，
#   单次请求最多要 21 个 offset → 21 个缓存条目（原来是 1 个）。
#   旧的「超 32 条就整个 clear()」策略会立刻被击穿（21+21=42 > 32），
#   导致缓存形同虚设（实测切一次买卖方向就退化回 1.3s）。
#   改为 **LRU 淘汰**：容量放大到能容下 3 品种 × 2 方向 × 21 档，且淘汰最少用的那条，
#   不再一刀切清空。
_CACHE_MAX = 256
_WINDOW_CACHE = OrderedDict()
_CACHE_HITS = 0
_CACHE_MISS = 0


def cache_stats():
    """排障用：看看缓存命中率。"""
    tot = _CACHE_HITS + _CACHE_MISS
    return {"size": len(_WINDOW_CACHE), "max": _CACHE_MAX,
            "hits": _CACHE_HITS, "miss": _CACHE_MISS,
            "hit_rate": round(_CACHE_HITS / tot * 100, 1) if tot else None}


def clear_cache():
    _WINDOW_CACHE.clear()


def build_window_cached(conn, variety: str, cp: str = "P", months: int = 12,
                        offset: int = 0, tmax: int = 10):
    """
    带缓存的 build_window。历史数据不会变，键里带上「库内数据末尾日期」即可失效。
    实测冷算约 1.5s（120 次查询），缓存后 0ms —— 前端要反复切 T-N/档位，必须缓存。
    """
    global _CACHE_HITS, _CACHE_MISS
    key = (variety, cp, months, offset, tmax, store.latest_date(conn, variety))
    hit = _WINDOW_CACHE.get(key)
    if hit is not None:
        _CACHE_HITS += 1
        _WINDOW_CACHE.move_to_end(key)        # LRU：标记为最近使用
        return hit
    _CACHE_MISS += 1
    val = build_window(conn, variety, cp, months, offset, tmax)
    _WINDOW_CACHE[key] = val
    _WINDOW_CACHE.move_to_end(key)
    while len(_WINDOW_CACHE) > _CACHE_MAX:
        _WINDOW_CACHE.popitem(last=False)     # 淘汰最久未用的
    return val


# ------------------------------------------------------------------ 阈值
#
# ★ 2026-09-27 改版：主判据改为「**用最近 N 个月的历史真实数据定阈值**」
#   expect（黄灯线）= 该 T-N 分布的 **中位数 P50** —— 达到常态水平，可移可等
#   good  （绿灯线）= 该 T-N 分布的 **75% 分位 P75** —— 明显偏厚，可以考虑移仓
#
#   为什么不再用固定经验值：见 docs/005。
#   参考作者的实盘阈值是**随行情漂移**的 —— 2024-01-31 他说 MO 卖沽按月移仓
#   「大部分时候在 70-100 左右波动，超过 100 可以了」；到 2026-08-19 他的参考值
#   已经是 **200**。若把 100 写死，两年后就是错的（这正是本次发现的问题）。
#   用滚动窗口的 P50/P75，阈值会自动跟着市场走。
#
#   ⚠️ 注意口径：参考值 200 说的是 **MO 深实值档**（7800/8000）的数字；
#   本程序算的是**平值档**。两者量级不同 —— 实值越深、价差会被内在价值压低，
#   所以不能拿实值档的数字直接套平值档。
#   【历史核对记录】2026-09-16 参考程序在 MO 平值档（当时现货附近）的实盘是 174，
#   当时本程序近 12 个月平值档的 P75 = 171.1 —— 两边吻合，见 docs/005 §5。
#   （此后行情下移，平值档定格也随之下移，此处不再写死具体点位，
#     以免下次读代码的人把旧快照当成现在的口径。）
#
#   下面这张表**降级为兜底**：仅当历史样本不足（< MIN_SAMPLES）时使用。
#   ★ 铁律：必须 **good > expect**。判灯逻辑是
#       net >= good   → 绿灯
#       net >= expect → 黄灯
#       否则          → 红灯
#     若 good <= expect，中间那段会**直接跳过黄灯**（60~70 连"可移可等"都判不上），
#     是硬 bug。2026-09-28 修：IO 原为 good=60/expect=70（倒挂），已对调。
THRESHOLDS = {
    "IO": {"good": 70, "expect": 60, "note": "平值移仓价差 ≥ 70 点算厚，≥ 60 点可移可等"},
    "MO": {"good": 100, "expect": 70, "note": "卖沽按月 常态 70~100，≥ 100 可以考虑"},
    "HO": {"good": 60, "expect": 50, "note": "（参考 IO，量级相近）"},
}

# ★ 2026-09-30：ETF 期权的兜底阈值**按现货比例**给，不写死元数。
#   原因：ETF 期权价差单位是「元/份」，量级随标的价走 ——
#     E300 现货 4.43 → 平值价差约 0.05 元；E500 现货 7.50 → 约 0.10 元。
#   写死 0.05 到了 7.5 的标的上就废了，写死 0.10 到了 4.4 的标的上又永远红灯。
#   而价差 ≈ 时间价值差 ≈ S·σ·√(T/252)·φ(d)，**天然与现货成正比**。
#   比例取 MO 的口径反推：good 100/7300 ≈ 1.4%、expect 70/7300 ≈ 1.0%
#   （MO 是流动性最好、最校准的品种）。
#   ⚠️ 仅在「历史样本不足」时生效；一旦 ETF 攒够 MIN_SAMPLES 个月度样本，
#     自动切换到历史 P50/P75，这张比例表就不再参与判灯。
FALLBACK_RATIO = {"good": 0.014, "expect": 0.010}

# 单位（文案用）：兜底阈值来自 THRESHOLDS 时用「点」，按比例算出来时用「元」
def _unit_of(variety: str) -> str:
    return (config.VARIETIES.get(variety) or {}).get("strike_unit") or "点"

# 样本少于此数就不敢拿分位数当阈值（12 个样本的窗口，留一半余量）
MIN_SAMPLES = 6


def judge(net, variety: str, pct=None, dte_near=None, months=None, stats=None,
          t_n=None, spot=None):
    """
    给一个实时价差 → 灯 + 建议。

    · net      : 价差（股指=点 / ETF=元）
    · pct      : 该值在历史同 T-N 分布里的分位（0~100）
    · dte_near : 当月合约剩余自然日
    · months   : 历史窗口长度（月）。**必须传** —— 分位是相对位置，
                 参照系（多长的窗口）不写出来，读者无法判断这个分位意味着什么。
    · stats    : 该 T-N 的历史分布统计（需含 median / p75 / sample）。
                 ★ **给了就用它当阈值**（用历史真实数据做参考）；
                   没给、或样本数 < MIN_SAMPLES，则回退到 THRESHOLDS 的经验值。
    · t_n      : 当前对齐的 T-N（仅用于文案）
    · spot     : 现货（ETF 兜底阈值按比例算时必需；股指走 THRESHOLDS 用不到）
    """
    lights, reasons = [], []
    win = f"近 {months} 个月" if months else "历史同窗口"
    tn = f"T-{t_n} " if t_n else ""
    unit = _unit_of(variety)

    if net is None:
        return {"light": "gray", "text": "无报价", "reasons": []}

    # ---- 阈值：优先用历史真实分布，样本不足才回退经验值 ----
    dyn = bool(stats and stats.get("sample", 0) >= MIN_SAMPLES
               and stats.get("median") is not None and stats.get("p75") is not None)
    if dyn:
        expect = float(stats["median"])          # P50
        good = float(stats["p75"])               # P75
        n_s = stats["sample"]
        src = f"{win}的{tn}分布（{n_s} 个样本）"
        tag_hi = f"≥ {win}{tn}的 75% 分位 {good:.1f} {unit}"
        tag_mid = f"≥ {win}{tn}的中位 {expect:.1f} {unit}"
        if good < expect:
            # 分布本身不可能 P75 < P50；走到这里说明 stats 传坏了，绝不能静默出灯
            return {"light": "gray", "text": "阈值异常",
                    "reasons": [f"历史分布异常：P75({good:.1f}) < P50({expect:.1f})，已停止判灯"],
                    "threshold": round(good, 1), "expect": round(expect, 1),
                    "source": src, "dynamic": False, "sample": n_s}
    else:
        th = THRESHOLDS.get(variety)
        if th is None:
            # ★ 未配置品种（当前 = ETF 期权）：按现货比例推兜底阈值。
            #   没有 spot 就不猜 —— 宁可判灰灯，也不编一个数出来。
            if spot:
                good = float(spot) * FALLBACK_RATIO["good"]
                expect = float(spot) * FALLBACK_RATIO["expect"]
                src = (f"ETF 期权暂无历史样本，按现货 {spot:.3g} {unit} 的 "
                       f"{FALLBACK_RATIO['good']*100:.1f}% / {FALLBACK_RATIO['expect']*100:.1f}% 估")
            else:
                return {"light": "gray", "text": "暂无阈值",
                        "reasons": [f"{variety} 既无历史样本、也没给现货，无法定阈值"],
                        "dynamic": False, "sample": None}
        else:
            expect = float(th["expect"])
            good = float(th["good"])
            src = th["note"]
        # ★ 兜底表倒挂防线：good 必须 > expect，否则黄灯区间为空（见文件头铁律）。
        #   这里**返回异常灯**而不是抛异常 —— judge() 在 HTTP 请求路径上，
        #   抛出去会让整页 500。宁可这一项显示"阈值配置异常"，也不拖垮整页。
        if good <= expect:
            return {"light": "gray", "text": "阈值配置异常",
                    "reasons": [f"THRESHOLDS['{variety}'] 倒挂：good({good:.0f}) 必须 > "
                                f"expect({expect:.0f})，否则中间区间会跳过黄灯。"
                                f"请修正 core/expiry_window.py 的 THRESHOLDS。"],
                    "threshold": round(good, 1), "expect": round(expect, 1),
                    "source": "兜底阈值配置错误", "dynamic": False, "sample": None}
        tag_hi = f"≥ 经验阈值 {good:.2f} {unit}"
        tag_mid = f"≥ 期望 {expect:.2f} {unit}"

    # ---- 判灯：单一口径（阈值本身就来自历史分布，不再叠加分位灯，避免同义重复）----
    pct_txt = f"，处于 {pct:.0f}% 分位" if pct is not None else ""
    nd = 2 if unit == "元" else 1
    hi_name = f"{win}{tn}的 75% 分位" if dyn else f"{good:.{nd}f} {unit}"
    if net >= good:
        lights.append("green")
        reasons.append(f"价差 {net:.{nd}f} {unit} {tag_hi}（明显偏厚）")
    elif net >= expect:
        lights.append("yellow")
        reasons.append(f"价差 {net:.{nd}f} {unit} {tag_mid}，但未到 {hi_name}（常态水平）")
    else:
        lights.append("red")
        reasons.append(f"价差 {net:.{nd}f} {unit} < {win}{tn}的中位 {expect:.{nd}f} {unit}（偏薄）")

    if pct is not None:
        reasons.append(f"参照系：{src}{pct_txt}")

    if dte_near is not None and dte_near <= 7:
        reasons.append(f"当月仅剩 {dte_near} 天，进入集中移仓窗口")

    order = {"green": 2, "yellow": 1, "red": 0}
    light = max(lights, key=lambda x: order[x]) if lights else "gray"
    text = {"green": "可以考虑移仓", "yellow": "可移可等", "red": "再等等"}[light]
    return {"light": light, "text": text, "reasons": reasons,
            "threshold": round(good, 4), "expect": round(expect, 4),
            "source": src, "dynamic": dyn, "sample": (stats or {}).get("sample")}
