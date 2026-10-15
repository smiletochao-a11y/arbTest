# -*- coding: utf-8 -*-
"""
移仓价差计算（核心业务逻辑）

口径（源自公开实盘发言 + 他贴出的日报截图）：
  · 义务仓：买回当月 + 卖出次月
  · 收盘价口径（零成交用结算价）
  · 净收 = 次月收盘 − 当月收盘          ← 这就是「移仓价差」
  · 同行权价 → 内在价值抵消 → 净收 = 次月时间价值 − 当月时间价值
  · IV 用 Black-76 反推；远期 F 用 put-call 平价反推（免分红率假设）
  · 评级 = 剩余到期天数 + IV 期限结构（次月 IV 高→早移；低→移晚）
"""
import math
from datetime import date, datetime, timedelta

from core import store

R_DEFAULT = 0.02          # 无风险利率（对 IV 影响很小）
IV_ATM_WINDOW = 14        # 「平值 N 档」用于算 IV 中位数（复刻他的口径）


# ------------------------------------------------------------------ 基础数学

def _ndist(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expiry_date(expiry_code: str, ref_year: int = None,
                weekday: int = 4, week_number: int = 3) -> date:
    """
    由到期月代码算**理论**到期日。

    '2610'（默认 weekday=4 周五 / week_number=3）→ 2026-10 的**第三个周五**
        （中金所股指期权/期货规则）

    参数化（2026-09 加 ETF 期权）：
      · 股指期权：weekday=4（周五）/ week_number=3（第三个）→ 第三个周五
      · ETF 期权：weekday=2（周三）/ week_number=4（第四个）→ 第四个周三
        （上交所/深交所 ETF 期权规则）

    ⚠️ 这只是"理论"日期。规则是「遇国家法定假日顺延」，
       2026-02-20（春节）和 2026-06-19（端午）都撞上了第三个周五，
       实测顺延到 02-24 / 06-22。
       → 需要**实际**到期日请用 `effective_expiry()`（数据驱动，自动含顺延）。
    """
    yy = 2000 + int(expiry_code[:2])
    mm = int(expiry_code[2:])
    if ref_year and yy < ref_year - 5:      # 跨年容错
        yy += 100
    first = date(yy, mm, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (week_number - 1))


def effective_expiry(conn, variety: str, expiry_code: str) -> str:
    """
    合约的**实际最后交易日**（ISO 字符串），自动含法定假日顺延。

    ★ 不维护节假日表（会过期、会出错），改成**数据驱动**：
      · 理论到期日 <= 库内数据末尾 → 取库里该合约**最后一个有数据的日期**
        （这就是中金所给出的真实到期日，顺延信息天然在里面）
      · 合约还没到期（理论到期日 > 数据末尾）→ 退回第三个周五
        （实测 2026-10 ~ 2027-09 的第三个周五都不撞法定假日，够用）

    2026-09-27 实测：2602 理论 2026-02-20 → 实际 **2026-02-24**；
                    2606 理论 2026-06-19 → 实际 **2026-06-22**。
    修正前用理论日期，26/06 的 T-1 价差算出 233.2，修正后 242.0（与参考程序一致）。
    """
    from core import config as _cfg
    _vm = _cfg.VARIETIES.get(variety, {})
    _wk = _vm.get("expiry_weekday", 4)
    _wn = _vm.get("expiry_weeknum", 3)
    theo = expiry_date(expiry_code, weekday=_wk, week_number=_wn).isoformat()
    last = store.latest_date(conn, variety)
    if last and theo <= last:
        r = conn.execute(
            "SELECT MAX(date) d FROM opt_daily WHERE variety=? AND expiry=?",
            (variety, expiry_code)).fetchone()
        if r and r["d"]:
            return r["d"]
    return theo


def years_to_expiry(date_iso: str, expiry_code: str, exp_iso: str = None):
    """返回 (T 年化, dte 自然日)。exp_iso 可传实际到期日（含假日顺延）"""
    d0 = datetime.strptime(date_iso, "%Y-%m-%d").date()
    if exp_iso:
        d1 = datetime.strptime(exp_iso, "%Y-%m-%d").date()
    else:
        d1 = expiry_date(expiry_code, ref_year=d0.year)
    dte = (d1 - d0).days
    return max(dte, 0) / 365.0, dte


def black76(F, K, T, r, sigma, cp):
    """Black-76 定价（F=远期价）"""
    if T <= 0 or sigma <= 0:
        intr = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
        return intr
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    disc = math.exp(-r * T)
    if cp == "C":
        return disc * (F * _ndist(d1) - K * _ndist(d2))
    return disc * (K * _ndist(-d2) - F * _ndist(-d1))


def implied_vol(price, F, K, T, r, cp, lo=0.005, hi=3.0):
    """二分法反推 IV；无解返回 None"""
    if not price or price <= 0 or T <= 0 or F <= 0:
        return None
    intr = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
    if price < intr * 0.5:          # 明显低于内在价值 → 数据异常
        return None
    f_lo = black76(F, K, T, r, lo, cp) - price
    f_hi = black76(F, K, T, r, hi, cp) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, r, mid, cp) - price > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


def implied_forward(near_rows, T, r, spot=None):
    """
    用 put-call 平价反推远期：F = K + (C − P)·e^{rT}

    ★ 只跳过「**确定 0 成交**」的档位（2026-09-27 实测 + 2026-09-30 修正）：
      零成交档位的收盘价不可靠（可能是上一笔或结算价），参与反推会污染 F。
      实测次月 ATM 附近有零成交档，不筛会让 IV 偏差从 1.2 拉到 1.4 vol点。
    取 ATM 附近若干档的中位数，更稳。
    """
    by_k = {}
    for o in near_rows:
        # ★★ 2026-09-30 修（东哥）：判据由 `not o.get("volume")` 改成 `== 0`。
        #
        #   原写法把「**字段缺失**（None）」也当成了「零成交」——
        #   而上交所 ETF 期权日行情**根本没有成交量字段**（库里 opt_daily.volume 全为 NULL）：
        #       E500 94 行 / volume 有值 0 行 / NULL 94 行；IO 59550 行全有值
        #   → 这条过滤把 ETF 的**所有档位**都跳过 → cands 为空 → 兜底 return spot
        #   → 贴水 = spot − spot = 0 → 年化恒 0（读者问的「ETF期权没有年化数据吗」就是这个）。
        #
        #   改 `== 0` 之后：
        #     · 股指（volume 全是整数）行为**逐位不变** —— None 从不出现；
        #     · ETF（volume 是 None）不再被误杀。
        #   零成交档的价格污染由下方「离现货最近的 5 档取中位数」兜住（实测已排除离群值）。
        if o.get("volume") == 0:
            continue
        px = o.get("close") if o.get("close") is not None else o.get("settle")
        if px:
            by_k.setdefault(o["strike"], {})[o["cp"]] = px
    cands = []
    for k, v in by_k.items():
        c, p = v.get("C"), v.get("P")
        if c and p:
            cands.append((abs(k - (spot or k)), k + (c - p) * math.exp(r * T)))
    if not cands:
        return spot
    cands.sort(key=lambda x: x[0])
    take = [f for _, f in cands[:max(1, min(len(cands), 5))]]
    take.sort()
    return take[len(take) // 2]


def _px(o):
    """
    价格口径：**零成交用结算价**，否则用收盘价。

    ★ 这是他的口径（截图备注原文：「P5100、P5200 今日零成交，已按结算价口径」）。
    实测（2026-09-14，IO2610-P-5100）：成交量 0 / 今收盘 616.8 / 今结算 653.8
    → 他表里写 653.8，即**结算价**。用收盘价会算错。
    """
    if not o.get("volume"):                 # 0 或 None
        return o.get("settle")
    return o.get("close") if o.get("close") is not None else o.get("settle")


# ------------------------------------------------------------------ 表构建

def build_table(conn, variety: str, date_iso: str, cp: str = "P",
                near: str = None, far: str = None, r: float = R_DEFAULT):
    """
    生成移仓价差表。

    返回 dict：
      variety/date/cp/near/far/spot/forward/dte_near/dte_far
      rows: [{strike, state, buyback, sell, net, net_pct, iv_near, iv_far, tv_near, tv_far, theta_far}]
      rating: {level, reason, iv_gap}
      notes: [...]
    """
    exps = store.load_expiries(conn, variety, date_iso)
    if len(exps) < 2:
        return {"ok": False, "error": f"{variety} {date_iso} 到期月不足 2 个：{exps}"}
    near = near or exps[0]
    far = far or exps[1]

    # ★ 2026-09-27：语义校验 —— 买回腿必须**早于**卖出腿，否则价差恒为负、毫无意义。
    #   前端已放开区间自选（东哥要的），用户很容易选反，所以在**这里**直接拦住，
    #   不给"悄悄返回一个负价差"的机会（第一性：不让错的状态流到下游）。
    if near not in exps:
        return {"ok": False, "error": f"{near} 不是 {variety} 在 {date_iso} 的可选到期月：{exps}"}
    if far not in exps:
        return {"ok": False, "error": f"{far} 不是 {variety} 在 {date_iso} 的可选到期月：{exps}"}
    if exps.index(near) >= exps.index(far):
        return {"ok": False,
                "error": f"移仓区间选反了：买回腿 {near} 必须早于卖出腿 {far}（可用：{' → '.join(exps)}）"}

    near_rows = [o for o in store.load_options(conn, variety, date_iso, cp) if o["expiry"] == near]
    far_rows = [o for o in store.load_options(conn, variety, date_iso, cp) if o["expiry"] == far]
    if not near_rows or not far_rows:
        return {"ok": False, "error": f"缺少 {near} 或 {far} 的{cp}行情"}

    # 远期反推必须 C、P 成对 → 单独取一份不过滤 cp 的
    all_rows = store.load_options(conn, variety, date_iso)
    near_all = [o for o in all_rows if o["expiry"] == near]
    far_all = [o for o in all_rows if o["expiry"] == far]

    from core import config as _cfg
    idx_symbol = _cfg.VARIETIES.get(variety, {}).get("index")
    spot = store.load_index_close(conn, idx_symbol, date_iso) if idx_symbol else None

    T_n, dte_n = years_to_expiry(date_iso, near, effective_expiry(conn, variety, near))
    T_f, dte_f = years_to_expiry(date_iso, far, effective_expiry(conn, variety, far))

    # 真远期（put-call 平价反推）→ 只用来算「贴水」，**不用于 IV**
    F_true = implied_forward(near_all, T_n, r, spot=spot)
    F_true_far = implied_forward(far_all, T_f, r, spot=spot)

    # ★★ IV 用的远期 = **标的指数收盘价**（不是上面的真远期）
    #
    # 实测（2026-09-14 IO 卖沽，与他 21 档 IV 逐档比对，2026-09-27）：
    #     F = 指数收盘价      → IV 平均偏差 1.03 vol点，最大 5.16
    #     F = 平价真远期      → IV 平均偏差 4.27 vol点，最大 17.94
    #       （2610 真远期比现货低 27.7 点，代入后次月 IV 系统性掉 3 vol点）
    # → **他的 IV 就是「现货价口径」**，与他「按收盘价计算」的声明一致。
    # ⚠️ 别「顺手修好」成真远期，会让 IV 系统性偏离他的口径。
    F = spot
    F_far = spot

    near_by_k = {o["strike"]: o for o in near_rows}
    far_by_k = {o["strike"]: o for o in far_rows}

    rows = []
    for k in sorted(set(near_by_k) | set(far_by_k)):
        n, f = near_by_k.get(k), far_by_k.get(k)
        if not n or not f:
            continue
        pn, pf = _px(n), _px(f)
        if pn is None or pf is None:
            continue
        net = pf - pn
        intr = max(k - (spot or F), 0.0)          # 认沽内在价值（按现货口径）
        net = round(net, 6)                        # 保留足够精度（ETF 元/份需 4 位，前端再按小数位显示）
        rows.append({
            "strike": k,
            "state": ("实值" if (spot and k > spot) else "虚值"),
            "buyback": pn,
            "sell": pf,
            "net": net,
            "net_pct": (round(net / spot * 100, 3) if spot else None),
            # IV 与价差同口径（收盘价，零成交用结算价）—— 他原话「按收盘价计算」
            "iv_near": _pct(implied_vol(pn, F, k, T_n, r, cp)),
            "iv_far": _pct(implied_vol(pf, F_far, k, T_f, r, cp)),
            "tv_near": round(max(pn - intr, 0.0), 2),
            "tv_far": round(max(pf - intr, 0.0), 2),
            "theta_far": round(max(pf - intr, 0.0) / dte_f, 2) if dte_f > 0 else None,
            # ★ 只把「明确 0 成交」当零成交；ETF 期权官方端点不给成交量（None），
            #   不能再当成零成交灰掉整行（close 与 settle 在该端点相等，价格可靠）。
            "zero_vol": (n.get("volume") == 0) or (f.get("volume") == 0),
        })

    rating = _rate(rows, spot, dte_n, dte_f)

    # ★ 每品种合约参数（ETF 与股指的乘数/单位不同，前端要靠它算名义、定显示小数位）
    #   ⚠️ 必须挪到算「贴水」之前 —— 贴水的小数位要按品种走（见下）。
    _vm = _cfg.VARIETIES.get(variety, {})
    _unit = _vm.get("strike_unit", "点")
    _mult = _vm.get("multiplier", 100)
    _strike_dec = 3 if _unit == "元" else 0      # ETF 行权价 元(3位) / 股指 点(整数)
    _prem_dec = 4 if _unit == "元" else 1        # ETF 权利金 元/份(4位) / 股指 点(1位)
    _net_rnd = 4 if _unit == "元" else 2         # 移仓价差(净收)保留小数位
    _fwd_dec = 4 if _unit == "元" else 2         # 远期/贴水 元(4位) / 点(2位)
    # 贴水百分比的小数位：股指保持 3 位（与修复前**逐位一致**，避免动到股指的任何数字），
    # ETF 用 4 位（贴水本身就只有 0.3~1.0%，3 位不够）。
    _pct_dec = 4 if _unit == "元" else 3

    # 贴水口径与 future 项目一致：**正数 = 贴水**（远期低于现货）
    #
    # ★★ 2026-09-30 修（东哥）：两个问题一起改。
    #   ① 小数位按品种走。原先一律 `round(spot - F, 2)` —— 对股指（2800~7300 点）
    #      无所谓，但对 ETF（1.6~7.4 **元**）2 位小数就是 **0.6% 的粒度**，
    #      比贴水本身还大。实测 E880：真值 0.0057 被舍成 0.01，
    #      年化从 2.24% 虚高到 3.95%（虚高 76%）。
    #   ② pct / annual 必须用**未舍入**的贴水算，最后一步才舍入。
    #      原先 `disc_far_pct = round(disc_far / spot * 100, 3)` 是拿**已舍入**的
    #      disc 再算，误差被二次放大。
    _disc_raw = (spot - F_true_far) if (F_true_far and spot) else None
    disc_far = round(_disc_raw, _fwd_dec) if _disc_raw is not None else None
    disc_far_pct = (round(_disc_raw / spot * 100, _pct_dec)
                    if _disc_raw is not None and spot else None)
    disc_far_annual = (round(_disc_raw / spot * 100 * 365 / dte_f, 2)
                       if _disc_raw is not None and spot and dte_f > 0 else None)

    return {
        "ok": True,
        "variety": variety, "date": date_iso, "cp": cp,
        # ★ 合约参数（前端算名义/显示用）
        "multiplier": _mult,
        "unit": _unit,
        "strike_decimals": _strike_dec,
        "premium_decimals": _prem_dec,
        "discount_decimals": _fwd_dec,
        "near": near, "far": far,
        "spot": spot,
        # 真远期（平价反推）与「贴水」——A 方案扫描目标之一
        "forward": round(F_true, _fwd_dec) if F_true else None,
        "forward_far": round(F_true_far, _fwd_dec) if F_true_far else None,
        "discount_far": disc_far,            # 正数 = 贴水
        "discount_far_pct": disc_far_pct,
        "discount_far_annual": disc_far_annual,
        "dte_near": dte_n, "dte_far": dte_f,
        "rows": rows,
        "rating": rating,
        "notes": ["零成交档位已按结算价口径" if any(r["zero_vol"] for r in rows) else ""],
    }


def _pct(v):
    return None if v is None else round(v * 100, 2)


def _rate(rows, spot, dte_n, dte_f):
    """评级：剩余天数 + IV 期限结构（复刻他的两个变量）"""
    # 平值附近 N 档的 IV 差中位数
    valid = [r for r in rows if r["iv_near"] is not None and r["iv_far"] is not None]
    iv_gap = None
    if valid:
        if spot:
            valid.sort(key=lambda r: abs(r["strike"] - spot))
        take = valid[:IV_ATM_WINDOW]
        gaps = sorted(r["iv_near"] - r["iv_far"] for r in take)
        iv_gap = round(gaps[len(gaps) // 2], 2)

    if dte_n <= 7:
        level = "集中移仓"
        reason = f"距到期仅 {dte_n} 天，本周内应移完主力"
    elif iv_gap is not None and iv_gap < 0:
        level = "持有观察"
        reason = f"次月IV低 {abs(iv_gap)} vol点 → 移晚更有利（远月波动率便宜）"
    elif iv_gap is not None and iv_gap > 0:
        level = "可早移"
        reason = f"次月IV高 {iv_gap} vol点 → 早移可卖出更贵的波动率"
    else:
        level = "持有观察"
        reason = "IV 期限结构接近平水，无明确优势"

    return {"level": level, "reason": reason, "iv_gap": iv_gap,
            "window": f"平值{IV_ATM_WINDOW}档中位数"}
