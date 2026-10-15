# -*- coding: utf-8 -*-
"""
实时移仓价差（盘中决策用）

与 `migrate.py` 的分工：
    migrate  用**历史收盘价**算 → 复盘、基准（参考作者每天 17:00 那张表）
    live     用**当前实时行情**算 → 盘中决策

★★ 双口径（这是本模块存在的理由）

  · 名义价差   = 次月**最新价** − 当月**最新价**
                好看，但可能根本成交不了

  · 可成交价差 = 次月**买一(bid)** − 当月**卖一(ask)**
                卖次月 → 成交在对手的买一
                买回当月 → 成交在对手的卖一
                两条腿各被买卖价差刮一次 → 这才是真能拿到的钱

  为什么必须做这个：future 项目的期货-期权平价套利就是栽在这里 ——
  用中间价算，偏离 +3.9 ~ +20.9 点（看着有肉）；换成可成交价，
  12 个月份里只有 1 个还赚钱。**名义价差会系统性高估收益。**

★ 历史分位：把实时价差放进「历史同 T-N 窗口」的分布里看分位
  —— 单看"价差 220 点"没有意义，要知道它在历史上算厚还是薄。
"""
import re
from datetime import date, datetime, timedelta

from core import config, expiry_window as ew, migrate, quotes, sina, store

_OPT_RE = re.compile(r"^([A-Z]{2})(\d{4})-([CP])-(\d+)$")

# 历史窗口最大 T-N。
# ★ 2026-09-27 由 20 收窄到 10（东哥拍板）：只判 T-1~T-10。
#   业务理由：刚换月时当月时间价值还厚，「不是价差不够大，而是这时候根本不该移」，
#   看 T-11~T-20 只会在不该动的时候发信号，徒增折腾。
#   超出窗口 → aligned_T 置空 → 判灯置灰 + 前端提示「尚未进入移仓窗口」。
HIST_MAX = 10


# ------------------------------------------------------------------ 工具

def _num(x):
    return None if x is None else float(x)


def forward_from_chain(chain, month, cp_near="P", r=None):
    """
    用当月 put-call 平价反推**远期**：F = K + (C − P)·e^{rT}

    ★ 为什么用远期而不是现货：
      Black-76 里"平值"的定义就是 F = K。用现货定平值会系统性偏一档。
    ★ 只用「买卖价都有效」的档位，零成交/单边报价会污染 F。
    """
    r = migrate.R_DEFAULT if r is None else r
    T, _ = migrate.years_to_expiry(date.today().isoformat(), month)
    if T <= 0:
        T = 1 / 365.0
    ch = chain["chain"]
    ks = sorted({k for (m, cp, k) in ch if m == month})
    cands = []
    for k in ks:
        c, p = ch.get((month, "C", k)), ch.get((month, "P", k))
        if not c or not p:
            continue
        cl, pl = c.get("last"), p.get("last")
        if not cl or not pl:
            continue
        cands.append((k, k + (cl - pl) * (1 + r * T)))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[len(cands) // 2][1]


def workdays_until(day_iso, holidays=None):
    """从今天到 day_iso 之间的**工作日**数（不含今天，含目标日）。
    节假日表缺未来数据，用工作日近似 —— 短窗口够用。"""
    d0 = date.today()
    d1 = datetime.strptime(day_iso, "%Y-%m-%d").date()
    n = 0
    d = d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5 and (holidays is None or d.isoformat() not in holidays):
            n += 1
    return n


def holiday_set(conn, variety):
    """
    休市日集合 = 「历史反推」∪「未来公告表」

    · 历史部分：从指数日线反推「工作日但没有行情」的日子（不需要维护任何表）
    · 未来部分：读 `config.HOLIDAYS_FUTURE`（★ 没有行情可反推，必须人工补，见该处注释）

    ⚠️ 2026-09-27 踩坑：原来只做历史反推 → 未来的国庆 10-01~10-07 被当成交易日，
       当月剩余交易日从 10 虚报成 15，历史窗口错对齐到 T-15，分位从 50.0% 虚高到 77.3%。
    """
    sym = config.VARIETIES.get(variety, {}).get("index")
    if not sym:
        return set()
    rows = conn.execute("SELECT date FROM index_daily WHERE symbol=? ORDER BY date",
                        (sym,)).fetchall()
    have = {r["date"] for r in rows}
    if not have:
        return set()
    lo, hi = min(have), max(have)
    d = datetime.strptime(lo, "%Y-%m-%d").date()
    end = datetime.strptime(hi, "%Y-%m-%d").date()
    out = set()
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.add(d.isoformat())
        d += timedelta(days=1)

    for days in (config.HOLIDAYS_FUTURE or {}).values():
        out.update(days)
    return out


# ------------------------------------------------------------------ 主入口

def live_spread(conn, variety: str = "MO", cp: str = "P", months_hist: int = 12):
    """
    实时移仓价差 + 历史分位 + 建议。

    返回：
      variety/cp/near/far/spot(远期)/dte_near/dte_far/hist_T
      rows: [{strike, state, buyback, sell, nominal, executable, spread_cost,
              pct_hist, iv_near, iv_far, tv_near, tv_far, theta_far, tradable}]
      atm: 平值档那一行（决策主看这个）
      judge: {light, text, reasons, threshold, expect}
    """
    ch = quotes.fetch_option_chain(variety)
    months = ch["months"]
    if len(months) < 2:
        return {"ok": False, "error": f"{variety} 实时行情不足 2 个到期月：{months}"}
    near, far = months[0], months[1]

    # ★ F 的口径：**必须用现货**（不是平价反推的远期）
    #   为什么：IV 是拿 F 反解的，F 差 30 点，IV 就差约 1.7 个 vol 点。
    #   2026-09-27 与银河 APP 全 22 档逐档比对（MO2610/2611-P-7500 及各档）：
    #       F = 现货收盘   → 平均偏差 0.257 / 0.437 vol 点
    #       F = 平价远期   → 平均偏差 2.976 / 1.891 vol 点
    #   → **银河用现货，误差小 10 倍**。项目内另两处（migrate.py §196、expiry_window.spread_at §165）
    #     也都是现货口径，只有本函数原先用远期 —— 属于跑偏，本次拉回。
    #
    # ★★ 2026-09-28 修（东哥首次实盘发现）：
    #   现货**必须取实时行情**，不能取「库内最新收盘价」。
    #   踩坑现场：库天然滞后一天（收盘后才入库），9-28 中证1000 收 7294.17（跌 3.65%），
    #   程序却还在用 9-24 的 7570.15 当现货 → 平值档判成 **7600**（应为 **7300**），
    #   ②区「本月（今天）」柱、①区判灯取值全部跟着偏。
    #   **盘中更致命**：库里只有昨天的收盘价，实时页根本没法用。
    #   兜底顺序：实时指数 → 库内最新收盘 → 平价反推远期（仅前两者都失败时）。
    sym = config.VARIETIES.get(variety, {}).get("index")
    F = None
    f_source = f_time = spot_prev = None
    rt_err = None
    try:
        rt = sina.fetch_index_realtime(sym) if sym else None
    except Exception as e:  # noqa: BLE001 —— 实时接口挂了不能连累整页，但原因必须带出来
        rt, rt_err = None, f"{type(e).__name__}: {e}"
    if rt:
        F = rt["price"]
        f_source = "realtime"
        f_time = f'{rt["date"]} {rt["time"]}'
        spot_prev = rt.get("prev_close")
    if not F:
        _d_latest = store.latest_date(conn, variety)
        F = store.load_index_close(conn, sym, _d_latest) if sym else None
        if F:
            f_source, f_time = "db_close", _d_latest
    if not F:
        F = forward_from_chain(ch, near)
        if F:
            f_source, f_time = "chain_forward", None
    if not F:
        return {"ok": False, "error": "无法取得标的现价（实时/库内/平价反推全部失败）"}

    # 剩余天数
    from core import config as _lcfg
    _vm_live = _lcfg.VARIETIES.get(variety, {})
    _lwk = _lcfg.VARIETIES.get(variety, {}).get("expiry_weekday", 4)
    _lwn = _lcfg.VARIETIES.get(variety, {}).get("expiry_weeknum", 3)
    exp_n = migrate.effective_expiry(conn, variety, near) if _in_db(conn, variety, near) \
        else migrate.expiry_date(near, weekday=_lwk, week_number=_lwn).isoformat()
    exp_f = migrate.effective_expiry(conn, variety, far) if _in_db(conn, variety, far) \
        else migrate.expiry_date(far, weekday=_lwk, week_number=_lwn).isoformat()
    T_n, dte_n = migrate.years_to_expiry(date.today().isoformat(), near, exp_n)
    T_f, dte_f = migrate.years_to_expiry(date.today().isoformat(), far, exp_f)
    T_n = max(T_n, 1 / 365.0)
    T_f = max(T_f, 1 / 365.0)

    # 历史分布（按"剩余交易日"对齐 T-N）
    # ★ 2026-09-27 窗口收窄到 T-10（东哥拍板）：刚换月的时间价值还厚，
    #   「不是价差不够大，而是这时候根本不该移」，所以 T-11 以上不判灯。
    #   旁证：他给 IO 的经验线「> 70 点可以关注」，而 T-10 的 P75 = 70.3，几乎重合。
    holidays = holiday_set(conn, variety)
    n_trade = workdays_until(exp_n, holidays)
    win = ew.build_window_cached(conn, variety, cp, months_hist, 0, HIST_MAX)
    all_nets = (win.get("all_nets") or {}) if win.get("ok") else {}
    all_stats = (win.get("stats") or {}) if win.get("ok") else {}
    exact = str(n_trade) in all_nets
    if exact:
        aligned_T = n_trade
    else:
        # 窗口外（剩 11~20 个交易日）→ 不再「就近取」，置空 = 不判灯。
        aligned_T = None
    hist_nets = all_nets.get(str(aligned_T)) if aligned_T else None
    hist_stats = all_stats.get(str(aligned_T)) if aligned_T else None

    ks = sorted({k for (m, c, k) in ch["chain"] if m == near and c == cp}
                & {k for (m, c, k) in ch["chain"] if m == far and c == cp})
    if not ks:
        return {"ok": False, "error": f"{near}/{far} 没有共同的{cp}档位"}

    rows = []
    for k in ks:
        n = ch["chain"][(near, cp, k)]
        f = ch["chain"][(far, cp, k)]
        nl, fl = n.get("last"), f.get("last")
        nb, fa = n.get("bid"), f.get("ask")     # 买回当月 → 吃卖一
        fb, na = f.get("bid"), n.get("ask")     # 卖出次月 → 吃买一
        nominal = (fl - nl) if (nl and fl) else None
        executable = (fb - na) if (fb and na) else None
        rows.append({
            "strike": k,
            "state": "实值" if k > F else "虚值",
            "buyback": nl, "sell": fl,
            "buyback_ask": na, "sell_bid": fb,
            "nominal": round(nominal, 2) if nominal is not None else None,
            "executable": round(executable, 2) if executable is not None else None,
            "spread_cost": (round(nominal - executable, 2)
                            if nominal is not None and executable is not None else None),
            "iv_near": _iv(nl, F, k, T_n, cp),
            "iv_far": _iv(fl, F, k, T_f, cp),
            "tradable": bool(nb and fa and fb and na),
            "zero_vol": (n.get("volume") == 0) or (f.get("volume") == 0),
        })

    # ★ 平价理论认沽价（2026-09-30 试点，东哥指定先做 ETF 期权）
    #
    #   欧式平价：C − P = (F − K)·e^{−rT}   →   P_理论(K) = C(K) − (F − K)·e^{−rT}
    #   这里 F 用**该月全链**反推（forward_from_chain，已经把 e^{rT} 折进去了），
    #   所以写成 r=0 的形式：P_理论(K) = C(K) − F + K。
    #
    #   为什么要它：**远月的虚值认沽经常「市场价 = 0」**（当天一笔没成交，实时口就返回 0），
    #   而同一档的**认购**通常有价 —— 用认购把认沽的参考价补出来，移仓价差才不至于失真。
    #
    #   ⚠️ 三条纪律（已写入 docs/008 第九节）：
    #     1) 只对**认购有报价**的档位算；认购是 0 就无从反推；
    #     2) forward_from_chain 内部只用「认购、认沽都 > 0」的档位反推 F，零报价档不参与；
    #     3) 它给的是「与认购自洽」的价，**不是独立估值** —— 认购报价失真，它跟着失真。
    #     4) 近月基本没用（平价本来就成立，理论价≈市场价）；价值在远月。
    parity = {}
    for _m in (near, far):
        _Fm = forward_from_chain(ch, _m)
        if not _Fm:
            continue
        for k in ks:
            _c = ch["chain"].get((_m, "C", k))
            _cl = _c.get("last") if _c else None
            if _cl:
                parity[(_m, k)] = _cl - (_Fm - k)
    for r in rows:
        pn, pf = parity.get((near, r["strike"])), parity.get((far, r["strike"]))
        r["parity_near"] = round(pn, 4) if pn is not None else None
        r["parity_far"] = round(pf, 4) if pf is not None else None
        # ★ 2026-09-30 改口径（读者实际要的东西，东哥转述）：
        #   他要的是「**市场报价是不是高于理论价**」→ 判断现在卖沽合不合适。
        #   所以这里给 **市场 − 理论**（叫 premium，溢价），**不是** 理论 − 市场。
        #     溢价 > 0 → 市场报价高于理论价 → 卖沽划算（你卖得比"公允"贵）
        #     溢价 < 0 → 市场报价低于理论价 → 卖沽吃亏
        #   ⚠️ 市场价为 0/缺失（当天没成交）时给 None → 前端显示「市场无报价」，
        #      绝不能拿 0 去减，否则会造出一个巨大的假溢价。
        #   ⚠️ 口径边界：市场价用 `sell`（次月**最新价**，与「名义价差」同源）。
        #      可成交价（买一）只有股指有；ETF 走 SSE yunhq 没有买卖价。
        #      所以这一列对 ETF 是「最新价 vs 理论价」，不是「你能卖到的价 vs 理论价」。
        r["parity_far_premium"] = (round(r["sell"] - pf, 4)
                                   if (pf is not None and r["sell"]) else None)

    atm = min(rows, key=lambda r: abs(r["strike"] - F))

    # ★ 判灯锚点（方案 C 简化版，2026-09-27 东哥拍板，见 docs/005 §7.4）：
    #   取「平值档 ±1 档」中**可成交价差最大**的那一档作为判灯值。
    #   为什么不再用单一平值档：F 改用现货口径后，平值档会随标的波动而跳档
    #   （标的 7570 时，距 7500 有 70 点、距 7600 只有 30 点 —— 波动 40 点就换档，
    #    判灯跟着翻脸，但行情其实没变）。取相邻档最大值 = 更稳、也更贴近实操
    #   （人会在整数关口之间选，看「能拿到的最大价差」）。
    #   ⚠️ `atm` 字段本身保持不变（仍 = 距 F 最近的一档），用于页面显示与 IV 对齐。
    _idx = ks.index(atm["strike"])
    cand = rows[max(0, _idx - 1): _idx + 2]
    _pick = [r for r in cand
             if (r["executable"] if r["executable"] is not None else r["nominal"]) is not None]
    if not _pick:
        _pick = [atm]
    ref_row = max(_pick,
                  key=lambda r: r["executable"] if r["executable"] is not None else r["nominal"])
    ref = ref_row["executable"] if ref_row["executable"] is not None else ref_row["nominal"]
    # 分位：优先用可成交价（决策看真金白银）
    pct = ew.percentile_of(hist_nets, ref) if hist_nets else None
    judge_refs = [{"strike": r["strike"],
                   "value": (r["executable"] if r["executable"] is not None else r["nominal"]),
                   "used": r["strike"] == ref_row["strike"]} for r in cand]

    # ★ 每一档用**它自己 offset 的历史分布**算分位（2026-09-27 修）
    #   原先所有档共用平值档分布 → 深度虚/实值档的价差天然更小，
    #   拿它比平值档分布会被系统性压低（K=7000 差约 41.7 个百分点）。
    #   offset 定义与 expiry_window.spread_at 一致：按行权价升序，+1 = 更高一档，
    #   基准是「当月平值档」（以现货 F 定，与历史侧同口径）。
    atm_idx = ks.index(atm["strike"])
    _off_cache = {}
    for r in rows:
        off = ks.index(r["strike"]) - atm_idx
        if not hist_nets:
            r["pct_hist"] = None
            continue
        if off == 0:
            nets = hist_nets                      # 平值档：直接复用，省一次全量计算
        else:
            if off not in _off_cache:
                w = ew.build_window_cached(conn, variety, cp, months_hist, off, HIST_MAX)
                _off_cache[off] = ((w.get("all_nets") or {}) if w.get("ok") else {})
            nets = _off_cache[off].get(str(aligned_T))
        v = r["executable"] if r["executable"] is not None else r["nominal"]
        r["pct_hist"] = ew.percentile_of(nets, v) if nets else None
        r["pct_off"] = off                       # 便于前端/排障核对用的是哪一档分布

    # ★ 判灯阈值 = 该 T-N 的历史真实分布（P50 / P75），见 docs/005
    j = ew.judge(ref, variety, pct, dte_n, months_hist,
                 stats=hist_stats, t_n=aligned_T, spot=F)

    return {
        "ok": True,
        "variety": variety, "cp": cp,
        "near": near, "far": far,
        "exp_near": exp_n, "exp_far": exp_f,
        # ⚠️ `forward` 是历史字段名，实际装的是**现货**（F 口径见上方说明）。
        #    保留它只为兼容旧前端；新代码请用 `spot`。
        "forward": round(F, 2),
        "spot": round(F, 2),
        "spot_prev": round(spot_prev, 2) if spot_prev else None,
        # ★ 现货的来源与时间（2026-09-28 加）：页面必须显式标出，
        #   否则用户无法判断"这个平值档是按哪天的行情定的"。
        "spot_source": f_source,      # realtime / db_close / chain_forward
        "spot_time": f_time,
        "spot_error": rt_err,         # 实时接口失败原因（正常为 null）
        "today": date.today().isoformat(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dte_near": dte_n, "dte_far": dte_f, "trade_days_near": n_trade,
        # ★ 窗口外 aligned_T=None → hist_T 也返回 None（不再回退成 1）。
        #   回退成 1 会让前端显示「历史 12 个月的 T-1 窗口」，是误导。
        #   前端见到 hist_T=null 且有 hist_exact=False 时应显示「尚未进入移仓窗口」。
        "hist_T": aligned_T,
        "hist_window_max": HIST_MAX,     # 判灯的窗口上界（前端文案要引用）
        "hist_exact": exact,
        "hist_months": months_hist,      # 历史参照系的窗口长度（页面上必须显式标出）
        "hist_stats": hist_stats,
        "rows": rows,
        "atm": atm,
        # ★ 每品种合约参数（2026-09-30）：前端靠它定显示小数位 ——
        #   ETF 期权价格是 0.0155 这种量级，写死 1 位小数会全显示成 0.0。
        "unit": _vm_live.get("strike_unit", "点"),
        "multiplier": _vm_live.get("multiplier", 100),
        "strike_decimals": 3 if _vm_live.get("strike_unit") == "元" else 0,
        "premium_decimals": 4 if _vm_live.get("strike_unit") == "元" else 1,
        # ★ 平价理论价的口径说明（2026-09-30 试点）
        "parity_note": ("平价理论价 = 用同档认购 + 同月平价远期反推的认沽价："
                        "P = C − (F − K)。F 由该月全链的 C−P+K 中位数反推。"
                        "它给的是「与认购自洽」的参考价，不是独立估值；"
                        "近月基本等于市场价，价值在远月（那里市场价常常是 0）。"),
        # ★ 判灯锚点（见 docs/005 §7.4）：平值档 ±1 里取最大可成交价差，
        #   judge_refs 记录参与比较的档位与各自取值，供页面显式标注「判灯取值来自哪档」。
        "judge_ref": {"strike": ref_row["strike"], "value": round(ref, 2),
                      "candidates": judge_refs},
        "judge": j,
        "source": "中金所实时快照（quote_%s.txt）" % variety,
        "note": "可成交价差 = 次月买一 − 当月卖一；名义价差 = 最新价之差",
    }


def _iv(price, F, k, T, cp):
    if not price:
        return None
    v = migrate.implied_vol(price, F, k, T, migrate.R_DEFAULT, cp)
    return round(v * 100, 2) if v else None


def _in_db(conn, variety, expiry):
    r = conn.execute("SELECT 1 FROM opt_daily WHERE variety=? AND expiry=? LIMIT 1",
                     (variety, expiry)).fetchone()
    return bool(r)
