# -*- coding: utf-8 -*-
"""
期权持仓风险测算引擎

核心问题（东哥的需求）：
  ① 我这个持仓，如果市场发疯，最多亏多少？
  ② 补一张"废纸"（虚值保护）要花多少钱？

不做预测、不荐股、不自动下单。只做两件事：算账 + 摆数字。

---- 口径说明（务必记住）----
1. 股指期权合约乘数 = 指数点 × 100 元/点
2. 卖方保证金：中金所公式（见 margin_of_short）
3. 情景分析给的是【到期损益】还是【即时损益】要分清：
   - 即时损益：假设标的价格瞬间变化，用当前 IV 重估期权（这里用简化：平价插值）
   - 到期损益：假设持有到到期日
   本模块默认给【到期损益】（更保守、更好理解），另给一个即时近似。
"""
import math

from core import config

def _get_mult(variety):
    """取品种合约乘数（股指 100 / ETF 10000）。variety=None 时兜底 100。"""
    if variety:
        return config.VARIETIES.get(variety, {}).get("multiplier", 100)
    return config.MULTIPLIER


def _leg_mult(leg, variety=None):
    """单腿合约乘数：优先用**这条腿自己的品种**，没有才用调用方给的默认品种。
    ★ 2026-09-29：持仓腿品种可各不相同（各腿独立，不跟「① 行情」联动），
      所以乘数必须逐腿取，否则 ETF 腿(10000) 会被当成股指腿(100) 算，名义差 100 倍。"""
    return _get_mult(leg.get("variety") or variety)


# ------------------------------------------------------------------ 单腿
def leg_payoff_at_expiry(leg, spot, multiplier=100):
    """
    单腿到期损益（元）。
    leg: {cp:'C'|'P', strike, qty(+1多/-1空), premium(开仓权利金,正数)}
    spot: 到期时标的指数点位
    到期损益 = 内在价值 × 乘数 × 数量 + 已收/付权利金
    """
    cp, k = leg["cp"], leg["strike"]
    if cp == "C":
        intrinsic = max(spot - k, 0.0)
    else:
        intrinsic = max(k - spot, 0.0)

    qty = leg["qty"]                 # 正=买入(多), 负=卖出(空)
    prem = leg.get("premium") or 0.0  # 开仓权利金（正数）

    # 买入：付出 prem，得到 intrinsic；卖出：收到 prem，付出 intrinsic
    value = intrinsic * multiplier * abs(qty)
    if qty > 0:
        pnl = value - prem * multiplier * abs(qty)
    else:
        pnl = prem * multiplier * abs(qty) - value
    return pnl


def position_value_at_expiry(legs, spot, multiplier=None, variety=None):
    """组合到期损益。multiplier 给定则所有腿统一用；否则逐腿按自己的品种取。"""
    if multiplier is not None:
        return sum(leg_payoff_at_expiry(l, spot, multiplier) for l in legs)
    return sum(leg_payoff_at_expiry(l, spot, _leg_mult(l, variety)) for l in legs)


# ------------------------------------------------------------------ 即时估值
def option_value_now(spot, strike, cp, iv, dte_days, r=0.02):
    """
    用 Black-Scholes 估当前期权价值（元/点）。
    股指期权是欧式，BS 适用。
    iv: 年化隐含波动率（小数，如 0.20）
    dte_days: 剩余自然日
    """
    if dte_days <= 0:
        # 到期：内在价值
        return max(spot - strike, 0.0) if cp == "C" else max(strike - spot, 0.0)
    t = dte_days / 365.0
    if iv is None or iv <= 0:
        iv = 0.20
    sqt = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * sqt)
    d2 = d1 - iv * sqt
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    if cp == "C":
        return spot * nd1 - strike * math.exp(-r * t) * nd2
    else:
        return strike * math.exp(-r * t) * (1 - nd2) - spot * (1 - nd1)


def implied_vol_from_price(price, spot, strike, cp, dte_days, r=0.02):
    """
    由期权价格反推 IV（牛顿法 + 二分兜底）。返回年化小数或 None。
    """
    if price is None or price <= 0 or spot <= 0 or dte_days <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if cp == "C" else max(strike - spot, 0.0)
    if price < intrinsic - 1e-6:
        return None  # 价格低于内在价值（数据异常或深度实值）

    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        v = option_value_now(spot, strike, cp, mid, dte_days, r)
        if v > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def leg_value_now(leg, spot, iv, dte_days, r=0.02, multiplier=100):
    """单腿即时市值（元）"""
    v = option_value_now(spot, leg["strike"], leg["cp"], iv, dte_days, r)
    return v * multiplier * leg["qty"]


def position_pnl_now(legs, spot, iv_map, dte_days, r=0.02, multiplier=100):
    """
    即时损益：假设标的价格瞬间变到 spot，期权按 IV 重估。
    iv_map: {(cp,strike): iv}  取不到就用默认
    """
    total = 0.0
    for l in legs:
        iv = iv_map.get((l["cp"], l["strike"]), iv_map.get("_default", 0.20))
        now_v = option_value_now(spot, l["strike"], l["cp"], iv, dte_days, r) * multiplier * l["qty"]
        cost = (l.get("premium") or 0.0) * multiplier * l["qty"]
        total += now_v - cost
    return total


# ------------------------------------------------------------------ 保证金
def margin_of_short(spot, strike, cp, iv, dte_days, r=0.02, multiplier=100):
    """
    卖方保证金（元/张），简化标准公式：
      看涨/看跌：(权利金 + max(12%×标的 − 虚值额, 7%×标的)) × 乘数
    虚值额：看涨 = max(行权价 − 标的, 0)；看跌 = max(标的 − 行权价, 0)
    ★ 对 ETF 期权同样适用（比例一致，只是乘数换成 10000）。
    """
    prem = option_value_now(spot, strike, cp, iv, dte_days, r)
    if cp == "C":
        otm = max(strike - spot, 0.0)
    else:
        otm = max(spot - strike, 0.0)
    a = 0.12 * spot - otm
    b = 0.07 * spot
    return (prem + max(a, b)) * multiplier


def total_margin(legs, spot, iv_map, dte_days, r=0.02, multiplier=None, variety=None):
    tot = 0.0
    for l in legs:
        if l["qty"] < 0:  # 只有卖方占保证金
            iv = iv_map.get((l["cp"], l["strike"]), iv_map.get("_default", 0.20))
            m = multiplier if multiplier is not None else _leg_mult(l, variety)
            tot += margin_of_short(spot, l["strike"], l["cp"], iv, dte_days, r, m) * abs(l["qty"])
    return tot


# ------------------------------------------------------------------ "废纸"保护
def suggest_protection(chain, legs, spot, dte_days, ladder=None, r=0.02, multiplier=100):
    """
    针对持仓里的【裸卖认购】（无保护的卖购）推荐保护档位。

    对每个挡位返回：
      - 买哪张虚值认购（instrument）
      - 单价、共需多少钱
      - 补上后，上涨到该行权价以上时的最大亏损被锁在多少
    """
    ladder = ladder or config.LADDER
    iv_map = build_iv_map(chain, spot, dte_days, r)

    # 找出裸卖的认购腿（qty<0 且没有同期同量的多头认购覆盖）
    short_calls = [l for l in legs if l["cp"] == "C" and l["qty"] < 0]
    long_calls = [l for l in legs if l["cp"] == "C" and l["qty"] > 0]
    n_short = sum(-l["qty"] for l in short_calls)
    n_long = sum(l["qty"] for l in long_calls)
    naked = max(n_short - n_long, 0)

    out = {
        "naked_calls": naked,
        "has_protection": naked == 0,
        "ladder": [],
    }
    if naked == 0:
        return out

    # 用近月（剩余天数最少的到期月）
    months = getattr(chain, "months", None) or chain["months"]
    month = months[0]

    for pct in ladder:
        target = spot * (1 + pct / 100.0)
        strikes = [k for k in chain["strikes"]]
        if not strikes:
            continue
        # 选 >= target 的最小行权价（真虚值）
        cand = [k for k in strikes if k >= target]
        if not cand:
            continue
        k = min(cand)
        o = chain["chain"].get((month, "C", k))
        if not o:
            continue
        price = o.get("ask") or o.get("last")
        if price is None:
            continue
        unit_cost = price * multiplier                      # 买一张要多少钱
        total_cost = unit_cost * naked
        out["ladder"].append({
            "pct": pct,
            "strike": k,
            "instrument": o["instrument"],
            "month": month,
            "ask": o.get("ask"),
            "last": o.get("last"),
            "unit_cost": unit_cost,
            "total_cost": total_cost,
            "dte_days": dte_days,
            "iv": iv_map.get(("C", k)),
        })
    return out


# ------------------------------------------------------------------ IV
def build_iv_map(chain, spot, dte_days, r=0.02):
    """{ (cp,strike): iv }  + '_default'"""
    ivs = []
    out = {}
    for (m, cp, k), o in chain["chain"].items():
        # 只取近月
        if m != chain["months"][0]:
            continue
        px = o.get("last")
        if px is None or px <= 0:
            continue
        iv = implied_vol_from_price(px, spot, k, cp, dte_days, r)
        if iv and 0.02 < iv < 3.0:
            out[(cp, k)] = iv
            ivs.append(iv)
    out["_default"] = (sorted(ivs)[len(ivs) // 2] if ivs else 0.20)
    return out


# ------------------------------------------------------------------ 汇总
def analyze(chain, legs, dte_days, r=0.02, variety=None, chains=None):
    """
    主入口。返回完整风险报告。
    legs: [{cp, strike, qty, premium, month, variety?}]
    variety: 主品种码（用于行情链/IV/保护建议/情景曲线）；各腿可带自己的 variety（乘数逐腿取）。
    chains: {品种码: chain} —— 混品种持仓时，各腿的保证金要用**自己品种**的现货与 IV。
    """
    spot = chain["spot"]
    if spot is None:
        raise ValueError("无法反推标的现价（行情数据异常）")

    chains = chains or {}
    mult = _get_mult(variety)          # 主品种乘数（对外展示 / 保护建议用）

    # 每个品种一套（chain, iv_map），保证金按腿自己的品种取
    _ctx_cache = {}

    def _ctx(lv):
        if lv not in _ctx_cache:
            c = chains.get(lv) or chain
            _ctx_cache[lv] = (c, build_iv_map(c, c["spot"], dte_days, r))
        return _ctx_cache[lv]

    # 保证金：逐腿按自己的品种（现货 / IV / 乘数都取该品种的）
    margin = 0.0
    for l in legs:
        if l["qty"] >= 0:
            continue
        lv = l.get("variety") or variety
        c, im = _ctx(lv)
        if c.get("spot") is None:
            continue
        iv = im.get((l["cp"], l["strike"]), im.get("_default", 0.20))
        margin += margin_of_short(c["spot"], l["strike"], l["cp"], iv,
                                  dte_days, r, _leg_mult(l, variety)) * abs(l["qty"])

    # 情景分析（到期损益）
    # ★ 混品种持仓：不同标的**没有共同点位轴**，所以情景统一按「各标的同比例涨跌」定义，
    #   每条腿用**自己品种的现价** × (1+pct)。同品种时与原来逐位一致（零回归）。
    scen = []
    worst = None
    for pct in config.SCENARIOS:
        s = spot * (1 + pct / 100.0)
        pnl = 0.0
        for l in legs:
            lv = l.get("variety") or variety
            c, _im = _ctx(lv)
            s_l = (c.get("spot") or spot) * (1 + pct / 100.0)
            pnl += leg_payoff_at_expiry(l, s_l, _leg_mult(l, variety))
        row = {"pct": pct, "spot": s, "pnl": pnl}
        scen.append(row)
        if worst is None or pnl < worst["pnl"]:
            worst = row

    # 已收/已付权利金合计（逐腿乘数）
    net_premium = sum(-(l.get("premium") or 0.0) * _leg_mult(l, variety) * l["qty"] for l in legs)

    # 保护建议：只覆盖**主品种**的腿
    # （别的品种没有对应的行情链，给不出「买哪张、多少钱」，硬算会给出错的行权价/价格）
    main_legs = [l for l in legs if (l.get("variety") or variety) == variety]
    prot = suggest_protection(chain, main_legs, spot, dte_days, r=r, multiplier=mult)

    # 名义价值（东哥 2026-09-29 要）：按各腿的**行权价**算名义敞口
    #   名义价值 = 行权价 × 该腿品种的合约乘数 × 手数
    #   为什么用行权价不用现价：卖沽被行权时，你按**行权价**接货，那是真实要掏的钱。
    #   分正负：卖出(义务仓)=你**欠**的敞口，买入(权利仓)=你**持有**的敞口。
    leg_nominal = []
    short_nom = long_nom = 0.0
    for l in legs:
        v = l["strike"] * _leg_mult(l, variety) * abs(l["qty"])
        leg_nominal.append(v)
        if l["qty"] < 0:
            short_nom += v
        else:
            long_nom += v
    # 卖沽的"最坏接货金额"：只有认沽义务仓被行权才需要真的掏钱买现货
    put_short_nom = sum(l["strike"] * _leg_mult(l, variety) * abs(l["qty"])
                        for l in legs if l["qty"] < 0 and l["cp"] == "P")

    # ★ ETF 期权实物交割提示（Phase 2c）：按**各腿自己的品种**判断（不只看主品种）
    physical_note = None
    etf_put_short = [
        l for l in legs
        if l["qty"] < 0 and l["cp"] == "P"
        and config.VARIETIES.get(l.get("variety") or variety, {}).get("exchange") in ("SSE", "SZSE")
    ]
    if etf_put_short:
        amt = sum(l["strike"] * _leg_mult(l, variety) * abs(l["qty"]) for l in etf_put_short)
        physical_note = (
            "ETF期权为实物交割，卖沽被行权需按行权价买入对应 ETF 份额。"
            f"准备资金约 {amt:,.0f} 元。"
        )

    # ★ 混品种提示（东哥 2026-09-30）：不同标的没有共同点位轴，必须显式说明口径
    other_vs = sorted({(l.get("variety") or variety) for l in legs} - {variety})
    mixed_note = None
    if other_vs:
        def _nm(x):
            return config.VARIETIES.get(x, {}).get("name", x)
        mixed_note = (
            f"本次体检含多个品种（另有 {'、'.join(_nm(x) for x in other_vs)}）。"
            f"到期损益按「各标的同比例涨跌」计算（曲线横轴点位是主品种「{_nm(variety)}」的）；"
            f"补保护建议只覆盖主品种的裸卖认购。名义敞口与保证金已包含全部腿。"
        )

    return {
        "spot": spot,
        "dte_days": dte_days,
        "mixed_note": mixed_note,
        "nominal": {
            "short": short_nom,        # 义务仓名义敞口（卖出方向）
            "long": long_nom,          # 权利仓名义敞口（买入方向）
            "put_short": put_short_nom,  # ★ 卖沽被行权时要接货的钱
            "per_leg": leg_nominal,    # 与 legs 一一对应
        },
        "iv_default": _ctx(variety)[1].get("_default"),
        "legs": legs,
        "margin": margin,
        "margin_per_leg": margin / max(len([l for l in legs if l['qty'] < 0]), 1) if margin else 0,
        "net_premium": net_premium,
        "scenarios": scen,
        "worst": worst,
        "protection": prot,
        "months": chain["months"],
        "physical_note": physical_note,
        "multiplier": mult,
    }


if __name__ == "__main__":
    from core import quotes

    ch = quotes.fetch_option_chain("IO")
    spot = ch["spot"]
    print(f"标的(沪深300) = {spot:.2f}")

    # 模拟一个东哥描述过的持仓：裸卖购（无废纸保护）
    atm_k = min(ch["strikes"], key=lambda k: abs(k - spot))
    month = ch["months"][0]
    o = ch["chain"].get((month, "C", atm_k))
    print(f"模拟：卖出 10 张 {o['instrument']}，权利金 {o['last']}")

    legs = [{"cp": "C", "strike": atm_k, "qty": -10,
             "premium": o["last"], "month": month}]

    dte = 30
    rep = analyze(ch, legs, dte)
    print(f"\n保证金占用: {rep['margin']:,.0f} 元")
    print(f"净收权利金: {rep['net_premium']:,.0f} 元")
    print(f"最坏情景: {rep['worst']['pct']:+.0f}%  ->  {rep['worst']['pnl']:,.0f} 元")
    print("\n情景:")
    for s in rep["scenarios"]:
        print(f"   {s['pct']:+4.0f}%  点位 {s['spot']:>8.0f}   盈亏 {s['pnl']:>12,.0f}")
    print("\n废纸保护档位:")
    for l in rep["protection"]["ladder"]:
        print(f"   距现价 {l['pct']}%  买 {l['instrument']}  单价 {l['ask']}  "
              f"共 {l['total_cost']:,.0f} 元")
