# -*- coding: utf-8 -*-
"""
「入场判断」计算层 —— 回答三个问题（设计定稿见 docs/009 §4）：

  ① 该不该建/加多头敞口？  → 均线状态（一票否决）+ 点位分位（近 1/3/10 年三档并列）
  ② 收的钱够不够赔？        → ATM IV 分位 + VRP（IV − 条件前瞻 RV）
  ③ 仓位有没有超标？        → 风险度（需要本金，由前端算，本模块不管）

★ 口径铁律（不许改，改了数字会静默错）：
  1. **IV 一律用 `etf_iv` 的 `atm_iv`**（= 官方认购/认沽平均），绝不单边。
     理由见 docs/009 §2：官方模型 F=S·e^{rT} 无视分红，认购偏低/认沽偏高。
  2. **点位分位必须三档并列**。只给一档一定误导 —— 实测沪深300 可以同时是
     「近1年最低位」和「近10年偏高位」（见 docs/009 §4.4）。
  3. **这是「价格分位」，不是「估值分位」**。本地没有 PE/PB/ERP，给不了估值结论。
     沪深300 从 2005 年 818 涨到 4357（+433%），价格分位天然偏高。
  4. **RV 用对数收益年化**（×√252），窗口 20/60 为主，120/250 只作参考。
  5. **VRP = IV − 条件前瞻 RV**，绝不能用 trailing RV 当基准 ——
     低波动会回归，拿"当前 20 日 RV"当分母会系统性高估溢价（docs/055 §3.5）。
"""
import math
import threading
import time
from datetime import date, datetime

from core import etf_iv, sina, store

# ---------------------------------------------------------------- 标的映射

# ETF 标的 -> 对应指数（算均线/分位/RV 用）
UND_INDEX = {
    "510050": ("sh000016", "上证50"),
    "510300": ("sh000300", "沪深300"),
    "510500": ("sh000905", "中证500"),
    "588000": ("sh000688", "科创50"),
    "588080": ("sh000688", "科创50"),
    "159915": ("sz399006", "创业板指"),
}

# 均线窗口。判断「所有均线之下」时用全部这些窗口
MA_WINDOWS = (5, 10, 20, 60, 120, 250)

# 点位分位要并列的三个区间
PCT_YEARS = (1, 3, 10)

# VRP：用「20 日 RV 同水平」的历史时点，看随后多少天的实际 RV
VRP_HORIZON = 60
VRP_TOL_REL = 0.15        # 相对容差 ±15%
VRP_TOL_ABS = 0.02        # 绝对容差下限 ±2pp（低波动时防止窗口太窄）

# 指数日线抓取深度（4000 根 ≈ 2010 年至今，足够算近 10 年分位）
HIST_N = 4000
_HIST_TTL = 1800          # 秒。进程内缓存，避免每次开页面都抓 6 个指数
_HIST_CACHE = {}
_HIST_LOCK = threading.Lock()

# 页面「溢价 · VRP」那一格的说明（东哥 2026-10-01 晚要求：要能看懂）
VRP_NOTE = (
    "**VRP = 用「实际波动」验一下期权到底贵不贵。**\n"
    "IV 是市场**报价**的波动率（藏在期权价格里），RV 是**事后真发生**的波动率。"
    "卖方**收的是 IV、赔的是 RV**，所以赚不赚就看这两个谁大。\n"
    "\n"
    "**前瞻 RV** 是「往后看」的实际波动：先翻历史，找出"
    "**和今天波动水平差不多的那些时点**，再看它们**之后 60 天**实际波动了多少，取平均。\n"
    "⚠️ 不能拿「过去 60 天」当基准 —— 那段时间正好包含当前这段行情，等于自己跟自己比。\n"
    "\n"
    "**怎么读**：VRP = IV − 前瞻 RV。**正数** = 收的比赔的多，卖方占优；"
    "**负数** = 收的钱盖不住实际波动，**倒亏**。"
)


# ---------------------------------------------------------------- 指数历史

def ensure_index_history(conn, symbol: str, n: int = HIST_N, force: bool = False):
    """把指数日线抓进 index_daily（幂等 INSERT OR REPLACE）。返回写入行数。

    进程内 TTL 缓存：同一次会话里反复开页面不会重复抓。
    注意：**新数据要等 TTL 过期或重启服务**（与项目其他缓存一致）。
    """
    now = time.time()
    with _HIST_LOCK:
        hit = _HIST_CACHE.get(symbol)
        if hit and not force and now - hit < _HIST_TTL:
            return 0
    rows = sina.fetch_index_daily(symbol, n=n)
    if not rows:
        return 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily (date,symbol,close,updated_at) VALUES (?,?,?,?)",
        [(r["day"], symbol, r["close"], ts) for r in rows])
    conn.commit()
    with _HIST_LOCK:
        _HIST_CACHE[symbol] = now
    return len(rows)


def _closes(conn, symbol: str):
    """[(date, close), ...] 升序"""
    return [(r["date"], r["close"]) for r in conn.execute(
        "SELECT date, close FROM index_daily WHERE symbol=? AND close IS NOT NULL "
        "ORDER BY date", (symbol,))]


def _trading_gap(d_from: str, d_to: str):
    """两个 ISO 日期之间的自然日差（只为提示数据新鲜度，不追求交易日精确）。"""
    try:
        return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 指标

def ma_state(closes):
    """均线状态。closes = [float] 升序。

    返回 {ma5:.., ..., above_all:bool, below_all:bool, n_above:int, n_below:int,
          verdict:'above_all'|'below_all'|'mixed'}

    ★ 「所有均线之下」= 收盘价 < 每一条均线。这是 docs/050 §一 那条纪律的判据：
      「所有均线下方**不裸卖沽**，所有均线上方不卖购」（⚠️ 指裸卖）。
    """
    cur = closes[-1]
    mas = {}
    for w in MA_WINDOWS:
        if len(closes) >= w:
            mas[w] = sum(closes[-w:]) / w
    above = sum(1 for v in mas.values() if cur > v)
    below = sum(1 for v in mas.values() if cur < v)
    if not mas:
        verdict = "unknown"
    elif below == len(mas):
        verdict = "below_all"
    elif above == len(mas):
        verdict = "above_all"
    else:
        verdict = "mixed"
    return {"ma": mas, "above_all": above == len(mas) and bool(mas),
            "below_all": below == len(mas) and bool(mas),
            "n_above": above, "n_below": below, "n_ma": len(mas), "verdict": verdict}


def price_percentile(closes, years: int):
    """当前点位在「近 N 年」里的分位（0~100）。

    分位 = 有多少天收盘低于今天 ÷ 区间交易日数。
    ★ 这是**价格分位**，不是估值分位（见模块 docstring 第 3 条）。
    """
    n = int(years * 243)                     # 每年约 243 个交易日
    win = closes[-n:] if len(closes) > n else closes
    if len(win) < 2:
        return None
    cur = win[-1]
    return sum(1 for c in win if c < cur) / len(win) * 100.0


def realized_vol(closes, window: int):
    """年化已实现波动率：日对数收益标准差 × √252。"""
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def _rv_series(closes, window: int):
    """滚动 RV 序列：[(index, rv)]，index 对应 closes 的下标（窗口末端）。"""
    out = []
    if len(closes) < window + 1:
        return out
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0
                    for i in range(1, len(closes))]
    s = sum(rets[1:window + 1])
    s2 = sum(r * r for r in rets[1:window + 1])
    k = window
    for i in range(window, len(closes)):
        if i > window:
            out_r, in_r = rets[i - window], rets[i]
            s += in_r - out_r
            s2 += in_r * in_r - out_r * out_r
        m = s / k
        var = max((s2 - k * m * m) / (k - 1), 0.0)
        out.append((i, math.sqrt(var) * math.sqrt(252.0)))
    return out


def conditional_forward_rv(closes, cur_rv, horizon=VRP_HORIZON,
                           tol_rel=VRP_TOL_REL, tol_abs=VRP_TOL_ABS):
    """★ VRP 的正解（docs/055 §3.5）。

    找出历史上「20 日 RV ≈ 当前水平」的所有时点，统计**随后 `horizon` 天**实际发生的 RV。

    返回 {n, med, mean, lo, hi}（比例，不是百分数）或 None。

    ⚠️ 为什么不能直接用 trailing RV 当基准：低波动会回归，拿"当前 20 日 RV"当分母
       会系统性高估溢价。必须看"同类时点后来实际发生了什么"。
    ⚠️ 报「均值」比「中位」更保守也更正确：卖方损益按期望累积，而波动率分布右偏。
    """
    if cur_rv is None:
        return None
    series = _rv_series(closes, 20)
    if not series:
        return None
    tol = max(cur_rv * tol_rel, tol_abs)
    fwd = []
    for i, rv in series:
        if abs(rv - cur_rv) > tol:
            continue
        if i + horizon >= len(closes):
            continue
        seg = closes[i:i + horizon + 1]
        if len(seg) < horizon + 1:
            continue
        rets = [math.log(seg[j] / seg[j - 1]) for j in range(1, len(seg))
                if seg[j - 1] > 0]
        if len(rets) < 2:
            continue
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        fwd.append(math.sqrt(var) * math.sqrt(252.0))
    if len(fwd) < 20:
        return None
    fwd.sort()
    return {"n": len(fwd), "med": fwd[len(fwd) // 2],
            "mean": sum(fwd) / len(fwd), "lo": fwd[int(len(fwd) * .1)],
            "hi": fwd[int(len(fwd) * .9)]}


# ---------------------------------------------------------------- 汇总

def build_entry(conn, underlying: str, fetch_index: bool = True):
    """把一个标的的入场判断所需全部数字算出来。返回 dict。"""
    underlying = underlying.upper()
    sym, idx_name = UND_INDEX.get(underlying, (None, "?"))
    out = {"underlying": underlying, "index_symbol": sym, "index_name": idx_name,
           "name": etf_iv.UNDERLYING_NAME.get(underlying, underlying),
           "ok": False}

    # --- IV（库里的 atm_iv，唯一口径）
    latest = etf_iv.latest(conn, underlying)
    if latest:
        out["iv"] = {
            "date": latest["date"], "expiry": latest.get("expiry"),
            "dte": latest.get("dte"), "atm_iv": latest.get("atm_iv"),
            "atm_call": latest.get("atm_call"), "atm_put": latest.get("atm_put"),
            "strike": latest.get("strike"), "src": latest.get("src"),
        }
        for label, win in (("all", None), ("y1", 243), ("y3", 243 * 3)):
            p = etf_iv.percentile(conn, underlying, window=win)
            out["iv"][f"pct_{label}"] = p["pct"] if p else None
            if label == "all" and p:
                # ★ 「分位」是排名百分比，p50 等是「IV 数值」—— 两把尺子必须都下发。
                #   只给分位会让用户拿 14.49%（IV）去和 20.7%（分位）比大小（2026-10-01 踩过）
                for k in ("p10", "p25", "p50", "p75", "p90"):
                    out["iv"][k] = p.get(k)
        n = conn.execute("SELECT COUNT(*) c FROM etf_iv_daily WHERE underlying=?",
                         (underlying,)).fetchone()["c"]
        out["iv"]["n"] = n
        first = conn.execute("SELECT MIN(date) d FROM etf_iv_daily WHERE underlying=?",
                             (underlying,)).fetchone()["d"]
        out["iv"]["from"] = first

    # --- 指数（均线 / 点位分位 / RV）
    #
    # ★★ 必须锚定到「IV 的那一天」（2026-10-01 修）：
    #   IV 是这页最稀缺的输入（每天 1 个请求、只有一个值）。如果拿"库内最新指数日"
    #   配"库内最新 IV 日"，两边日期一旦不一致，**VRP 会直接失真** ——
    #   实测重抓中途：IV 还是 2020-07-23 的 26.37%，RV 已是 2026-09-30 的 13.44%，
    #   VRP 算出 +8.6pp（真值应为负）。所以统一截断到 IV 当天。
    #   代价：IV 若滞后，本页整体滞后 —— 但**至少是自洽的**，且日期会显示在页面上。
    ref_date = (out.get("iv") or {}).get("date")
    ref_closes = []
    if sym:
        if fetch_index:
            try:
                ensure_index_history(conn, sym)
            except Exception:  # noqa: BLE001  抓不到就用库里的
                pass
        rows = _closes(conn, sym)
        if ref_date:
            rows = [r for r in rows if r[0] <= ref_date]
        if len(rows) > 260:
            closes = [c for _, c in rows]
            ref_closes = closes
            out["index"] = {
                "date": rows[-1][0], "close": closes[-1],
                "n": len(rows), "from": rows[0][0],
                "ma": ma_state(closes),
                "pct": {f"y{y}": price_percentile(closes, y) for y in PCT_YEARS},
                "rv": {f"d{w}": realized_vol(closes, w)
                       for w in (20, 60, 120, 250)},
            }
            # 数据新鲜度：IV 日 vs 指数库最新日，差太多要在页面上说出来
            all_rows = _closes(conn, sym)
            if all_rows and rows[-1][0] < all_rows[-1][0]:
                out["stale_days"] = _trading_gap(rows[-1][0], all_rows[-1][0])
                out["index_latest"] = all_rows[-1][0]
            out["ok"] = True

    # --- VRP（★ 必须用与 IV 同一天的那段指数序列，否则溢价是假的）
    if out.get("index") and out.get("iv") and out["iv"].get("atm_iv"):
        cur_rv = out["index"]["rv"].get("d20")
        cf = conditional_forward_rv(ref_closes, cur_rv)
        if cf:
            iv = out["iv"]["atm_iv"]
            out["vrp"] = {
                "n": cf["n"], "cur_rv": cur_rv,
                "fwd_med": cf["med"], "fwd_mean": cf["mean"],
                "fwd_lo": cf["lo"], "fwd_hi": cf["hi"],
                "iv_minus_med": iv - cf["med"],
                "iv_minus_mean": iv - cf["mean"],     # ★ 保守口径
            }
    return out


# ---------------------------------------------------------------- 结论措辞

def _pct_word(p):
    """把分位翻译成人话。"""
    if p is None:
        return "无数据"
    if p <= 5:
        return f"{p:.1f}% —— 区间最低"
    if p <= 20:
        return f"{p:.1f}% —— 很低"
    if p <= 40:
        return f"{p:.1f}% —— 偏低"
    if p <= 60:
        return f"{p:.1f}% —— 中间"
    if p <= 80:
        return f"{p:.1f}% —— 偏高"
    if p <= 95:
        return f"{p:.1f}% —— 很高"
    return f"{p:.1f}% —— 区间最高"


def conclude(e, all_ma=None):
    """★ 四段式一句话结论：能不能裸卖 + 位置好不好 + 钱厚不厚 → 所以怎么做。

    措辞定稿见 docs/009 §4.5。**全部由数字推导，不写死结论。**
    all_ma = 其他标的的均线状态（用于「四大指数全部…」这句汇总）。
    """
    idx = e.get("index") or {}
    ma = idx.get("ma") or {}
    iv = e.get("iv") or {}
    vrp = e.get("vrp") or {}
    pct = idx.get("pct") or {}
    name = e.get("name", e.get("underlying"))

    tags, detail = [], []

    # ---- ① 能不能裸卖（均线一票否决）
    verdict = ma.get("verdict")
    if verdict == "below_all":
        tags.append("不能裸卖沽")
        gate = (f"{e.get('index_name')} 收盘 {idx.get('close', 0):.2f} "
                f"在**全部 {ma.get('n_ma')} 条均线之下** → 按纪律**不裸卖沽**")
        can_naked_put = False
    elif verdict == "above_all":
        tags.append("不能裸卖购")
        gate = (f"{e.get('index_name')} 收盘 {idx.get('close', 0):.2f} "
                f"在**全部 {ma.get('n_ma')} 条均线之上** → 可裸卖沽，但**不卖购**")
        can_naked_put = True
    elif verdict == "mixed":
        tags.append("均线交错")
        gate = (f"{e.get('index_name')} 均线**交错**（{ma.get('n_above')} 条上 / "
                f"{ma.get('n_below')} 条下）→ 按他的原话「均线交错时可以双卖」")
        can_naked_put = True
    else:
        gate = "均线数据不足"
        can_naked_put = None
    if all_ma:
        n_below = sum(1 for v in all_ma.values() if v == "below_all")
        if n_below == len(all_ma):
            gate += f"；**全部 {len(all_ma)} 个跟踪指数都在所有均线之下**"
        else:
            gate += f"；{len(all_ma)} 个跟踪指数里 {n_below} 个在所有均线之下"

    # ---- ② 位置好不好（三档并列，不许只给一档）
    p1, p3, p10 = pct.get("y1"), pct.get("y3"), pct.get("y10")
    if p1 is not None and p10 is not None:
        detail.append(f"{e.get('index_name')} 点位分位：近1年 {_pct_word(p1)} / "
                      f"近3年 {_pct_word(p3)} / 近10年 {_pct_word(p10)}（**价格分位**）")
        if p1 <= 20 and p10 >= 60:
            tags.append("短期低位·长期偏高")
            pos = (f"**位置适合分批建多头、不适合梭哈** —— 近1年 {p1:.1f}%（低位）"
                   f"但近10年 {p10:.1f}%（偏高）")
        elif p1 <= 20:
            tags.append("位置偏低")
            pos = f"**位置偏低**（近1年 {p1:.1f}%、近10年 {p10:.1f}%）"
        elif p1 >= 80:
            tags.append("位置偏高")
            pos = f"**位置偏高**（近1年 {p1:.1f}%、近10年 {p10:.1f}%），建多头要克制"
        else:
            pos = f"位置中性（近1年 {p1:.1f}%、近10年 {p10:.1f}%）"
    else:
        pos = "点位分位无数据"

    # ---- ③ 钱厚不厚（IV 分位）
    pv = iv.get("pct_all")
    ivv = iv.get("atm_iv")
    if ivv is not None and pv is not None:
        detail.append(f"{name} ATM IV {ivv*100:.2f}%（{iv.get('date')}，"
                      f"{iv.get('expiry')} 到期 / 剩 {iv.get('dte')} 天），"
                      f"全历史 {_pct_word(pv)}，近1年 {_pct_word(iv.get('pct_y1'))}")
        if pv <= 20:
            tags.append("权利金偏薄")
            money = (f"**收的钱偏薄**（IV {ivv*100:.2f}%，全历史仅 {pv:.1f}% 分位）")
        elif pv <= 45:
            tags.append("权利金偏薄")
            money = f"**收的钱偏薄**（IV {ivv*100:.2f}%，全历史 {pv:.1f}% 分位）"
        elif pv >= 80:
            tags.append("权利金很厚")
            money = f"**收的钱很厚**（IV {ivv*100:.2f}%，全历史 {pv:.1f}% 分位）"
        else:
            money = f"权利金中性（IV {ivv*100:.2f}%，全历史 {pv:.1f}% 分位）"
    else:
        money = "IV 无数据"

    # ---- ④ 所以怎么做
    acts = []
    if can_naked_put is False:
        acts.append("**要卖就配保护**（买虚值认沽 / 铁鹰式），不要裸卖")
    elif can_naked_put is True and pv is not None and pv <= 45:
        acts.append("可以卖沽，但**钱不厚，仓位要轻**")
    if vrp and vrp.get("iv_minus_mean") is not None:
        vm = vrp["iv_minus_mean"]
        detail.append(f"VRP（IV − 条件前瞻 RV 均值，样本 {vrp['n']} 个同类时点）："
                      f"**{vm*100:+.1f} pp**（IV {ivv*100:.2f}% − 前瞻 RV 均值 "
                      f"{vrp['fwd_mean']*100:.2f}%）；前瞻 RV 区间 "
                      f"{vrp['fwd_lo']*100:.1f}~{vrp['fwd_hi']*100:.1f}%")
        if vm <= 0:
            acts.append("VRP 为负 —— 收的钱盖不住实际波动，**不划算**")
        elif vm < 0.02:
            acts.append("VRP 很薄，**没有安全垫**")
    if p1 is not None and p1 <= 20:
        acts.append("**不升波就缓加仓**（分批、慢加，别一次打满）")
    if not acts:
        acts.append("没有明显信号，**按纪律持币等待**")

    return {
        "tags": tags,
        "gate": gate,
        "position": pos,
        "money": money,
        "action": "；".join(acts) + "。",
        "detail": detail,
        "can_naked_put": can_naked_put,
        "one_liner": " · ".join(tags) if tags else "无结论",
    }


def entry_report(conn, underlying: str, fetch_index: bool = True):
    """给页面的完整结果：本标的详情 + 四大指数均线汇总 + 四段式结论。"""
    e = build_entry(conn, underlying, fetch_index=fetch_index)

    # 四大指数（去掉重复的科创50 双胞胎）的均线状态
    # ★ 必须和主卡片用**同一个参考日**（ref_date = IV 的那天），否则一页上混两个日期，
    #   会自相矛盾（实测：主卡片 2024-04-30 判"均线交错"，而 peer 表用 2026-09-30
    #   判"全部在均线之下"）。锚定不到 ref_date 的指数就跳过，不硬凑。
    ref_date = (e.get("iv") or {}).get("date")
    all_ma, peers = {}, []
    seen = set()
    for und, (sym, idx_name) in UND_INDEX.items():
        if sym in seen:
            continue
        seen.add(sym)
        if fetch_index:
            try:
                ensure_index_history(conn, sym)
            except Exception:  # noqa: BLE001  抓不到就用库里的
                pass
        rows = _closes(conn, sym)
        if ref_date:
            rows = [r for r in rows if r[0] <= ref_date]
        if len(rows) > 260:
            st = ma_state([c for _, c in rows])
            all_ma[und] = st["verdict"]
            peers.append({"underlying": und, "index_name": idx_name,
                          "symbol": sym, "close": rows[-1][1], "date": rows[-1][0],
                          "verdict": st["verdict"], "n_below": st["n_below"],
                          "n_above": st["n_above"], "n_ma": st["n_ma"],
                          "pct": {f"y{y}": price_percentile([c for _, c in rows], y)
                                  for y in PCT_YEARS}})
    out = dict(e)
    out["peers"] = peers
    out["all_ma"] = all_ma
    out["conclusion"] = conclude(e, all_ma=all_ma)
    # ★ 数据新鲜度提示：本页整体锚定在「IV 的那一天」，若指数已有更新的数据，
    #   说明 IV 抓取可能失败/滞后，必须在页面上说出来（否则用户以为看的是今天）
    if e.get("stale_days") and e["stale_days"] > 3:
        out["conclusion"]["warning"] = (
            f"⚠️ 本页数据锚定在 **{(e.get('iv') or {}).get('date')}**（IV 的日期），"
            f"但指数已更新到 {e.get('index_latest')} —— 差 {e['stale_days']} 个自然日。"
            f"说明 ETF 期权 IV 抓取可能失败或滞后，请先跑一次数据更新。")
    # ★ 页面 IV 那一格旁边必须显示的白话说明（东哥 2026-10-01 要求）
    out["caliber_note"] = etf_iv.CALIBER_NOTE
    # ★ VRP 那一格的说明（东哥 2026-10-01 晚要求：VRP / 前瞻 RV 也要能看懂）
    out["vrp_note"] = VRP_NOTE
    return out
