# -*- coding: utf-8 -*-
"""
ETF 期权隐含波动率（IV）日度序列 —— **全历史可回溯**

背景（见 docs/009）：
  以前认为「ETF 期权只有 2 天数据，算不出 IV 分位」。这是错的。
  上交所「期权风险指标」接口从 **2015-02-09** 起提供逐合约官方 IV，且认 trade_date。

两个源，一个口径：
  ┌ 上交所 510050 / 510300 / 510500 / 588000 / 588080
  │     → 直接取官方 IV（IMPLC_VOLATLTY），无需自算
  └ 深交所 159915（创业板ETF）
        → 官方**没有** IV，也没有历史（option_drhy 无视日期）
        → 用当日合约链自算：平价反推远期 F → Black-76 反解 IV
        → 历史只能从「入库那天」起累积（东哥已确认：从今天起存，未来就有）

★★ 口径铁律（docs/009 第二节）：
  上交所算 IV 用的是 `F = S·e^{rT}`，**无视分红/贴水**，而市场的真实远期是递减的。
  后果：官方「认购 IV」系统性偏低、「认沽 IV」系统性偏高，偏差随到期放大
        （2610 各 ~1.5pp，2703 各 ~8pp）。
  ✅ 所以本模块**只输出 `atm_iv = (atm_call + atm_put) / 2`** —— 两侧偏差方向相反，
     取平均后抵消到 ±0.9pp 以内（近月 ±0.3pp）。
  ⛔ 任何地方都**不要**拿 `atm_put` 单边当「市场 IV」。
"""
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import calendar
from datetime import date, datetime, timedelta

from core import config, store

# ---------------------------------------------------------------- 常量

# 与项目其他模块一致：强制直连，不继承环境代理
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_SSL_CTX),
)

SSE_URL = "http://query.sse.com.cn/commonQuery.do"
SSE_SQLID = "SSE_ZQPZ_YSP_GGQQZSXT_YSHQ_QQFXZB_DATE_L"
SSE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "http://www.sse.com.cn/",
    "Host": "query.sse.com.cn",
}

# 有官方 IV 的标的（上交所）
SSE_UNDERLYINGS = ["510050", "510300", "510500", "588000", "588080"]

# 需要自算 IV 的标的（深交所）→ 对应 optarb 品种码
SZSE_UNDERLYINGS = {"159915": "E915"}

# 标的 -> 中文名（前端展示用）
UNDERLYING_NAME = {
    "510050": "上证50ETF",
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "588000": "科创50ETF（华夏）",
    "588080": "科创50ETF（易方达）",
    "159915": "创业板ETF",
}

# 期权上市日（决定历史起点，写死避免每次探测）
LISTED = {
    "510050": "2015-02-09",
    "510300": "2019-12-23",
    "510500": "2022-09-19",
    "588000": "2023-06-05",
    "588080": "2023-06-05",
    "159915": "2022-09-19",   # 创业板ETF期权（深交所）上市日
}

# CONTRACT_ID = 510300C2610M04400 / 510050C2401A02250（A = 分红调整过的合约）
_CONTRACT_RE = re.compile(r"^(\d{6})([CP])(\d{4})([A-Z])(\d+)$")

# 近月的定义：剩余天数落在这个区间才参与「最近月 ATM」
DTE_MIN = 5
DTE_MAX = 400

# ★ ATM 序列只认「最近月」：月合约本身寿命 ≤ 35 天，45 天给足长假余量。
#   超过 45 天说明近月数据缺失 —— 此时**宁可留空，也不要回退到季月/半年月**，
#   否则同一序列里混进 140 天的样本，分位就不可比了（2026-10-01 修）。
DTE_ATM_MAX = 45

# 定位平值档时，端点 delta 与 0.5 的最大容许残差（见 _atm_strike 的说明）
ATM_DELTA_TOL = 0.25

# ★ 页面「IV 那一格」旁边必须写死的白话说明（东哥 2026-10-01 要求：现在看不懂没关系，
#   将来要能看懂）。任何展示 IV 的地方都应该带上它。措辞定稿见 docs/009 §4.5。
CALIBER_NOTE = (
    "**ATM = At-The-Money「平值」**：行权价最接近当前股价的那一档期权。\n"
    "**ATM IV 就是这一档期权的隐含波动率** —— 市场报价里藏着的"
    "「未来一年大概会波动多少」。它也是卖方收钱的标尺："
    "**IV 越高，期权越贵，卖沽收的钱越多**。\n"
    "\n"
    "**分位 = 今天在历史里排第几便宜**。分位 20.7% = 历史上只有 20.7% 的日子比今天更便宜，"
    "**数字越小越便宜**。**历史中位**是历史所有日子的正中间那个数，"
    "比它低 = 比「平常」便宜。\n"
    "\n"
    "**为什么取「认购和认沽的平均」**：交易所算 IV 时**假设标的不分红**，"
    "但 ETF 实际是分红的 → **认购算低了、认沽算高了**"
    "（同一行权价能差 3~8 个百分点，月份越远差得越多）。"
    "这是**算法造成的假象**，不是市场真的这么看；两边一平均正好抵消（残差 < 1 个百分点）。"
)

# 自算 IV 时的无风险利率（只影响自算那条腿；平价反推的 F 对 r 极不敏感）
R_SELF = 0.015


# ---------------------------------------------------------------- 小工具

def _f(v):
    """'null' / '' / None -> None，否则 float"""
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "null", "None", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fourth_wednesday(year: int, month: int) -> date:
    """上交所/深交所 ETF 期权到期日 = 到期月第 4 个周三。"""
    weeks = [w[calendar.WEDNESDAY] for w in calendar.monthcalendar(year, month)
             if w[calendar.WEDNESDAY]]
    return date(year, month, weeks[3])


def expiry_date(yymm: str) -> date:
    """'2610' -> date(2026,10,28)"""
    return fourth_wednesday(2000 + int(yymm[:2]), int(yymm[2:]))


def dte_of(yymm: str, ref: date) -> int:
    return (expiry_date(yymm) - ref).days


# ---------------------------------------------------------------- 正态 / Black-76

def _N(x):
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _invN(p):
    lo, hi = -8.0, 8.0
    for _ in range(200):
        m = (lo + hi) / 2.0
        if _N(m) < p:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2.0


def _bs76(F, K, T, sigma, cp):
    """Black-76（不折现，只用 F）：返回理论价 / F 的比值×F。df 由调用方处理。"""
    from math import log, sqrt, exp
    if T <= 0 or sigma <= 0:
        return max(0.0, F - K) if cp == "C" else max(0.0, K - F)
    d1 = (log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if cp == "C":
        return exp(-R_SELF * T) * (F * _N(d1) - K * _N(d2))
    return exp(-R_SELF * T) * (K * _N(-d2) - F * _N(-d1))


def _iv76(px, F, K, T, cp):
    """由价格反解 IV（二分）。px 与理论价同口径（都含折现）。"""
    lo, hi = 1e-4, 5.0
    for _ in range(200):
        m = (lo + hi) / 2.0
        if _bs76(F, K, T, m, cp) > px:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2.0


def _interp(xs, ys, x):
    """xs 单调递增，线性插值（外推取端点）"""
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if xs[i] >= x:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _atm_strike(call_d, put_d, target=0.5, tol=ATM_DELTA_TOL):
    """
    定位「平值档」的行权价 K。

    把**认购 delta** 与 **「1 + 认沽 delta」** 合并成同一条等效曲线后，取 delta == target 处的 K。
    （Black-76 下 认沽 delta = 认购 delta − 1，所以 1+认沽delta 就是等效认购 delta。）

    ★ 为什么必须合并：极端日（熔断/涨停）官方行情只覆盖窄行权价带，单看认购侧可能
      **整条都跨不过 0.5**。实测 2016-01-07 的 510050 1601：认购最高 delta 只有 0.467，
      但认沽在同一批 K 上有等效 delta（1 − 0.514 = 0.486）—— 合并后即可定位。

    ★ 为什么不「跨不过就整月剔除」：近月行权价间距大时 delta 步长可达 0.34，
      ATM 档的 delta 天然可能落在 0.35~0.65。用 0.10 容差会把整个近月剔掉
      （这正是 2026-10-01 修掉的旧 bug）。这里只在**完全够不到平值**（端点残差 > tol）时才放弃。

    ★ 为什么 tol 取 0.25：粗网格半档 ≈ 0.17，留足余量；而「整条都是深虚值」（端点 delta 0.1）
      残差 0.4 > 0.25，仍会被正确剔除。
    """
    merged = {}
    for k, d in list(call_d) + [(k, 1.0 + d) for k, d in put_d]:
        if d is not None:
            merged.setdefault(k, []).append(d)
    if not merged:
        return None
    ks = sorted(merged)
    ds = [sum(merged[k]) / len(merged[k]) for k in ks]
    for i in range(1, len(ks)):
        if ds[i - 1] >= target >= ds[i]:
            span = ds[i - 1] - ds[i]
            t = 0.0 if span <= 0 else (ds[i - 1] - target) / span
            return ks[i - 1] + t * (ks[i] - ks[i - 1])
    # 没跨过 target：端点够得着平值就用端点（ATM 档本来就在端点附近）
    if abs(ds[0] - target) <= tol:
        return ks[0]
    if abs(ds[-1] - target) <= tol:
        return ks[-1]
    return None


# ---------------------------------------------------------------- 上交所：官方 IV

def fetch_sse_raw(date_iso: str, timeout: int = 25, retries: int = 2):
    """抓某一天的上交所期权风险指标原始 JSON。返回 result 数组或 None。"""
    d = date_iso.replace("-", "")
    qs = urllib.parse.urlencode({
        "isPagination": "false",
        "trade_date": d,          # ★ 下划线！不是 tradeDate
        "sqlId": SSE_SQLID,
        "contractSymbol": "",
    })
    last_err = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(f"{SSE_URL}?{qs}", headers=SSE_HEADERS)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            return data.get("result") or None
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries:
                time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"抓取上交所期权风险指标失败({date_iso}): {last_err}")


def parse_sse(rows, date_iso: str):
    """
    官方 IV -> {underlying: rec}
    rec = {expiry, dte, strike, atm_call, atm_put, atm_iv, p25_iv, p10_iv}
    规则：取「最近月（DTE_MIN<=dte<=DTE_MAX）」的平值档；
         ATM 档 = 认购 delta 恰为 0.5 的行权价（在 delta 曲线上插值，两侧 IV 在该 K 上取）。

    ★★ 两个必须分开的曲线（2026-10-01 修）：
       「定位平值 K」用**全部 delta 有效的行**；
       「取 IV」只用 **IV 有效的行**（官方对深实值/无成交合约给 IV=0）。
       早先把 IV=0 的行连 delta 一起丢掉 → delta 曲线断在平值之上 → 跨不过 0.5
       → 整个近月被误剔、退到次月甚至半年月（实测 510500 2025-07-01 的 2507：
       有效 delta 最高只到 0.485，被判「无法插值」，实际当天选成了 dte=175 的 2512）。
    """
    from collections import defaultdict
    ref = date.fromisoformat(date_iso)
    by = defaultdict(list)
    for r in rows:
        m = _CONTRACT_RE.match((r.get("CONTRACT_ID") or "").strip())
        if not m or m.group(1) not in SSE_UNDERLYINGS:
            continue
        iv = _f(r.get("IMPLC_VOLATLTY"))
        de = _f(r.get("DELTA_VALUE"))
        if de is None:
            continue
        # ★ IV 无效（=0）的行**不能丢**：delta 依然有效，是 delta 曲线的端点。
        by[(m.group(1), m.group(3))].append(
            (int(m.group(5)) / 1000.0, m.group(2), de,
             iv if (iv is not None and iv > 0) else None))

    out = {}
    for (und, yymm), lst in by.items():
        dte = dte_of(yymm, ref)
        if dte < DTE_MIN or dte > DTE_ATM_MAX:
            continue
        # 定位平值 K：用全部 delta 有效的行（含 IV=0 的深实值行），认购/认沽合并
        call_d = sorted((k, de) for k, cp, de, iv in lst if cp == "C")
        put_d = sorted((k, de) for k, cp, de, iv in lst if cp == "P")
        # 取 IV：只用 IV 有效的行
        calls = sorted((k, de, iv) for k, cp, de, iv in lst if cp == "C" and iv is not None)
        puts = sorted((k, de, iv) for k, cp, de, iv in lst if cp == "P" and iv is not None)
        if len(calls) < 3 or len(puts) < 3:
            continue
        ck = [c[0] for c in calls]
        civ = [c[2] for c in calls]
        pk = [p[0] for p in puts]
        pde = [p[1] for p in puts]
        piv = [p[2] for p in puts]

        # 平值档 = 等效 delta 恰为 0.5 的行权价（认购/认沽合并后插值，不是「最近一档」）
        kstar = _atm_strike(call_d, put_d)
        if kstar is None:
            continue                              # 该月的行权价没跨过平值
        atm_call = _interp(ck, civ, kstar)        # _interp 超出端点时取端点值
        atm_put = _interp(pk, piv, kstar)

        # ⚠️ 这里**故意不做**「认购/认沽价差过大就否决」的闸门（2026-10-01 试过，已撤）。
        #   理由：取平均之所以成立，正是因为官方公式漏掉分红造成的误差在两侧
        #   **方向相反、大小相等** —— 所以价差再大（2015-09-02 达 99pp），
        #   平均后误差照样抵消（docs/009 §2.3 验证：价差 14pp 时残差仅 0.89pp）。
        #   用价差大小去否决它，等于把方法本身否掉。
        #   实测佐证：2015-08-31/09-01/09-02 三天均值 70.31 / 72.68 / 71.95 —— 序列平滑。
        #   （那几天单看认购 22~36% / 认沽 105~122%，任取一边都会得出完全相反的结论。）

        # 认沽侧 25Δ / 10Δ（用 |delta| 插值）
        xs = sorted(-x for x in pde)
        ys = [piv[i] for i in sorted(range(len(pde)), key=lambda i: -pde[i])]
        rec = {
            "expiry": yymm,
            "dte": dte,
            "strike": kstar,
            "atm_call": atm_call,
            "atm_put": atm_put,
            "atm_iv": (atm_call + atm_put) / 2.0,     # ★ 页面唯一口径
            "p25_iv": _interp(xs, ys, 0.25),
            "p10_iv": _interp(xs, ys, 0.10),
        }
        cur = out.get(und)
        if cur is None or dte < cur["dte"]:
            out[und] = rec
    return out


# ---------------------------------------------------------------- 深交所：自算 IV

def compute_iv_from_chain(rows):
    """
    由当日合约链自算 IV（官方无 IV 时用）。
    rows = [dict(cp, strike, close)] —— 同一天、同一品种的全部合约。
    返回 {expiry: rec}，rec 结构同 parse_sse。
    """
    from collections import defaultdict
    by = defaultdict(dict)
    for r in rows:
        px = r.get("close") or r.get("settle")
        if px is None or px <= 0:
            continue
        by[r["expiry"]][(r["cp"], float(r["strike"]))] = float(px)

    out = {}
    for yymm, chain in by.items():
        strikes = sorted({k for _, k in chain})
        calls = [(k, chain[("C", k)]) for k in strikes if ("C", k) in chain]
        puts = [(k, chain[("P", k)]) for k in strikes if ("P", k) in chain]
        if len(calls) < 3 or len(puts) < 3:
            continue
        pm = dict(puts)
        # ① 平价反推远期：F = (C - P)·e^{rT} + K（同行权价，取中位）
        #    ★ 这里 T 用「合约最后交易日」估算，只用相对量级，对 F 影响极小
        fs = []
        for k, cpx in calls:
            ppx = pm.get(k)
            if ppx is None or ppx <= 0.0001 or cpx <= 0.0001:
                continue
            fs.append((cpx - ppx) + k)
        if not fs:
            continue
        fs.sort()
        F = fs[len(fs) // 2]
        out[yymm] = {"F": F, "chain": chain}
    return out


def _szse_rec(chain_info, yymm: str, ref: date):
    """把 compute_iv_from_chain 的中间结果转成最终 rec（需要 T，所以单独一步）。"""
    dte = dte_of(yymm, ref)
    if dte < DTE_MIN or dte > DTE_ATM_MAX:
        return None
    T = dte / 365.0
    F = chain_info["F"]
    chain = chain_info["chain"]
    strikes = sorted({k for _, k in chain})

    calls, puts = [], []
    for k in strikes:
        if ("C", k) in chain:
            calls.append((k, _iv76(chain[("C", k)], F, k, T, "C")))
        if ("P", k) in chain:
            puts.append((k, _iv76(chain[("P", k)], F, k, T, "P")))
    if len(calls) < 3 or len(puts) < 3:
        return None

    # ATM 档 = 远期 F 本身（Black-76 下 delta=0.5 恰好在 K=F），IV 在行权价上插值
    ck = [k for k, _ in calls]
    civ = [iv for _, iv in calls]
    pk = [k for k, _ in puts]
    piv = [iv for _, iv in puts]
    kstar = F
    atm_call = _interp(ck, civ, F)
    atm_put = _interp(pk, piv, F)
    if atm_call is None or atm_put is None:
        return None

    # 认沽 25Δ / 10Δ：用 Black-76 的 delta 反查
    from math import log, sqrt
    xs, ys = [], []
    for k, iv in sorted(puts):
        d1 = (log(F / k) + 0.5 * iv * iv * T) / (iv * sqrt(T))
        xs.append(abs(_N(d1) - 1.0))          # |delta_put| = 1 - N(d1)
        ys.append(iv)
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    xs = [xs[i] for i in order]
    ys = [ys[i] for i in order]

    return {
        "expiry": yymm,
        "dte": dte,
        "strike": kstar,
        # ★ 2026-10-04 修 bug：这里原来写的是 civ / piv（**整条 list**），
        #   导致下一行 list / float 直接抛 TypeError：
        #     unsupported operand type(s) for /: 'list' and 'float'
        #   后果：深交所（E915）的 IV 一条都没入过库（etf_iv_daily 里 src='szse' 恒为 0 行）；
        #   更糟的是异常发生在 build_rows() 里，会把当天上交所那 5 个标的的 IV 一起带走。
        #   上面 426~427 行已经算好 atm_call / atm_put（float），这里必须用它俩。
        "atm_call": atm_call,
        "atm_put": atm_put,
        "atm_iv": (atm_call + atm_put) / 2.0,
        "p25_iv": _interp(xs, ys, 0.25),
        "p10_iv": _interp(xs, ys, 0.10),
    }


# ---------------------------------------------------------------- 组装 / 入库

def build_rows(conn, date_iso: str):
    """
    组装某一天所有标的的 IV 行。
    上交所走官方接口；深交所走本地 opt_daily 自算。
    返回 [dict, ...]（可直接 save_rows）
    """
    rows = []
    # ── 上交所：官方 IV
    try:
        raw = fetch_sse_raw(date_iso)
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        for und, rec in parse_sse(raw, date_iso).items():
            rows.append({"date": date_iso, "underlying": und, "src": "sse", **rec})

    # ── 深交所：自算 IV（历史只能从有 opt_daily 的那天起）
    ref = date.fromisoformat(date_iso)
    for und, variety in SZSE_UNDERLYINGS.items():
        chain_rows = [dict(r) for r in conn.execute(
            "SELECT expiry, cp, strike, close, settle FROM opt_daily WHERE variety=? AND date=?",
            (variety, date_iso))]
        if not chain_rows:
            continue
        infos = compute_iv_from_chain(chain_rows)
        best = None
        for yymm, info in infos.items():
            rec = _szse_rec(info, yymm, ref)
            if rec and (best is None or rec["dte"] < best["dte"]):
                best = rec
        if best:
            rows.append({"date": date_iso, "underlying": und, "src": "szse", **best})
    return rows


def save_rows(conn, rows):
    """幂等写入。"""
    if not rows:
        return 0
    ts = _now()
    conn.executemany(
        """INSERT OR REPLACE INTO etf_iv_daily
           (date, underlying, expiry, dte, strike, atm_call, atm_put, atm_iv,
            p25_iv, p10_iv, src, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r["date"], r["underlying"], r["expiry"], r["dte"], r["strike"],
          r["atm_call"], r["atm_put"], r["atm_iv"], r.get("p25_iv"), r.get("p10_iv"),
          r["src"], ts) for r in rows])
    conn.commit()
    return len(rows)


def fetch_and_save_day(conn, date_iso: str):
    """抓某一天并入库。返回写入行数（0 = 非交易日 / 无数据）。"""
    rows = build_rows(conn, date_iso)
    return save_rows(conn, rows)


# ---------------------------------------------------------------- 查询 / 分位

def load_series(conn, underlying: str, field: str = "atm_iv", end: str = None):
    """按日期升序返回 [(date, value), ...]"""
    sql = f"SELECT date, {field} v FROM etf_iv_daily WHERE underlying=? AND {field} IS NOT NULL"
    args = [underlying]
    if end:
        sql += " AND date<=?"
        args.append(end)
    sql += " ORDER BY date"
    return [(r["date"], r["v"]) for r in conn.execute(sql, args)]


def percentile(conn, underlying: str, window: int = None, field: str = "atm_iv"):
    """
    返回该标的当前值在历史中的分位（0~100）。
    window = 最近 N 个交易日；None = 全历史。
    返回 dict：{value, pct, n, p10, p25, p50, p75, p90, vmax, from, to}
    """
    s = load_series(conn, underlying, field)
    if not s:
        return None
    vals = [v for _, v in s]
    if window:
        s = s[-window:]
        vals = [v for _, v in s]
    cur = vals[-1]
    srt = sorted(vals)
    n = len(srt)

    def q(p):
        return srt[min(n - 1, int(p * n))]

    return {
        "value": cur,
        "pct": 100.0 * sum(1 for v in vals if v < cur) / n,
        "n": n,
        "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
        "vmax": srt[-1],
        "from": s[0][0], "to": s[-1][0],
    }


def latest(conn, underlying: str = None):
    """最新一天的 IV 行。underlying=None 时返回全部标的的列表。"""
    if underlying:
        r = conn.execute(
            "SELECT * FROM etf_iv_daily WHERE underlying=? ORDER BY date DESC LIMIT 1",
            (underlying,)).fetchone()
        return dict(r) if r else None
    d = conn.execute("SELECT MAX(date) d FROM etf_iv_daily").fetchone()["d"]
    if not d:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM etf_iv_daily WHERE date=? ORDER BY underlying", (d,))]


def stats(conn):
    r = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT underlying) u, MIN(date) a, MAX(date) b "
        "FROM etf_iv_daily").fetchone()
    return {"rows": r["n"], "underlyings": r["u"], "from": r["a"], "to": r["b"]}


# ---------------------------------------------------------------- 回补

def trading_days(start_iso: str, end_iso: str):
    """周一~周五（节假日由接口返回空自动跳过）"""
    d = date.fromisoformat(start_iso)
    e = date.fromisoformat(end_iso)
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def backfill(conn, start_iso: str, end_iso: str, workers: int = 6, progress=None):
    """
    全历史回补（只抓上交所；深交所无历史，只能从有 opt_daily 的日子自算）。
    返回写入行数。
    """
    from concurrent.futures import ThreadPoolExecutor
    days = trading_days(start_iso, end_iso)
    total = 0
    done = 0

    def _one(d):
        try:
            raw = fetch_sse_raw(d)
        except Exception:  # noqa: BLE001
            return []
        if not raw:
            return []
        return [{"date": d, "underlying": u, "src": "sse", **rec}
                for u, rec in parse_sse(raw, d).items()]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(_one, days):
            done += 1
            if progress and done % 100 == 0:
                progress(done, len(days))
            total += save_rows(conn, rows)
    return total


def update_latest(conn, max_back: int = 10):
    """
    抓「最近一个有数据的交易日」并入库（跨节假日自动回退）。
    每日增量就调这个 —— 1 个上交所请求 + 本地自算，成本几乎为零。
    返回 (date_iso|None, n_rows, rows)
    """
    cur = date.today()
    for _ in range(max_back):
        if cur.weekday() < 5:
            rows = build_rows(conn, cur.isoformat())
            if rows:
                return cur.isoformat(), save_rows(conn, rows), rows
        cur -= timedelta(days=1)
    return None, 0, []


def import_json(conn, path: str):
    """
    从 .tmp/iv_full*.json 导入（结构与 backfill 产出一致，用于免重抓的快速灌库）。
    JSON: {underlying: [[date, {expiry,dte,k,atm_call,atm_put,p25,p10}], ...]}
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = []
    for und, series in data.items():
        for d, rec in series:
            if rec.get("atm_call") is None or rec.get("atm_put") is None:
                continue
            # 缓存里的日期是 YYYYMMDD，统一成 ISO；exp 是到期日，转成 YYMM
            ds = d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:]}"
            exp = rec.get("expiry") or rec.get("exp") or ""
            if len(exp) >= 7 and "-" in exp:
                exp = exp[2:4] + exp[5:7]        # '2026-10-28' -> '2610'
            rows.append({
                "date": ds, "underlying": und, "src": "sse",
                "expiry": exp, "dte": rec["dte"], "strike": rec.get("k"),
                "atm_call": rec["atm_call"], "atm_put": rec["atm_put"],
                "atm_iv": (rec["atm_call"] + rec["atm_put"]) / 2.0,
                "p25_iv": rec.get("p25"), "p10_iv": rec.get("p10"),
            })
    return save_rows(conn, rows)


if __name__ == "__main__":
    c = store.connect()
    store.init_db(c)
    print("当前库状态:", stats(c))
    today = date.today().isoformat()
    n = fetch_and_save_day(c, today)
    print(f"抓取 {today} 写入 {n} 行")
    for r in latest(c):
        print("  ", r["underlying"], UNDERLYING_NAME.get(r["underlying"], ""),
              f"{r['atm_iv']*100:.2f}%", r["src"])
