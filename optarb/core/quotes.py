# -*- coding: utf-8 -*-
"""
中金所股指期权行情抓取

数据源：http://www.cffex.com.cn/quote_{IO|HO|MO}.txt
格式：  instrument,position,volume,lastprice,updown,bprice,bamount,sprice,samount
        IO2610-C-4000,32,14,436.60,-76.40,432.40,1,441.20,1

同时提供期货行情 quote_{IF|IH|IC|IM}.txt：
        instrument,openprice,highest,lowest,lastprice,updown,bprice,bamount,sprice,samount,volume,position
"""
import csv
import io
import json
import re
import threading
import time
import urllib.request

from core import config, market_calendar

# ---- 缓存 ----
_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ★ 2026-09-29：抓行情**强制直连**，绝不走环境代理。
#
# 为什么必须这样（东哥机器上真实踩到）：
#   环境变量里有 HTTP_PROXY=http://127.0.0.1:1249（WorkBuddy 沙箱代理），
#   urllib 默认继承它。但那个代理**只在 WorkBuddy 会话内活着** ——
#   东哥双击 start.bat 起服务时它已经不在了 → 连不上 →
#   `[Errno 11001] getaddrinfo failed`，整页行情全挂。
#
# 第一性原理：中金所是公开站点，抓它根本不需要代理。
# 与其"记得设 NO_PROXY"这种靠人记的约定，不如在代码层直接绕开代理，
# 这样无论谁、用什么方式起服务都稳。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=config.CFFEX_HEADERS)
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    # 中金所这个口是 UTF-8；兜底 gb18030
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _cached(key, fn, ttl=None):
    """★ 2026-10-03：默认 TTL 改成**按市场状态动态算**（见 core/market_calendar.py）。

    盘中（含集合竞价）= `config.QUOTE_CACHE_SEC`（默认 30 秒，真·实时）；
    午休 / 盘前 / 收盘后 / 周末 / 假期 = 直接设到「下一次可能变化」的那一刻
    —— 那段时间根本没有实时行情，反复爬中金所纯属浪费。
    **实测效果：整个周末只会爬 1 次**，而不是每 30 秒爬一次。

    显式传 `ttl=` 的调用方不受影响（仍可强制指定）。
    """
    if ttl is None:
        ttl = market_calendar.quote_ttl()
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, val)
    # 只在「非盘中」打一行 —— 用来确认周末/午休确实只抓了一次（盘中太吵，不打）
    if ttl > config.QUOTE_CACHE_SEC:
        st = market_calendar.market_state()
        print("[quotes] 抓取 %s（%s，本次缓存 %ds ≈ %.1fh）"
              % (key, market_calendar.STATE_LABEL.get(st, st), ttl, ttl / 3600.0), flush=True)
    return val


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------ 期权
# instrument 形如 IO2610-C-4000  ->  (month, cp, strike)
_INST_RE = re.compile(r"^([A-Z]{2})(\d{4})-([CP])-(\d+)$")

# SSE ETF 期权合约代码：510300C2610M04300 -> (underlying, cp, month, strike×1000)
_ETF_OPT_RE = re.compile(r"^(\d{6})([CP])(\d{4})M(\d+)$", re.IGNORECASE)


def parse_option_symbol(inst):
    """IO2610-C-4000 -> dict(variety, month, cp, strike) 或 None"""
    m = _INST_RE.match(inst.strip())
    if not m:
        return None
    return {
        "variety": m.group(1),
        "month": m.group(2),          # 2610
        "cp": m.group(3),             # C / P
        "strike": int(m.group(4)),    # 4000
    }


def _sina_spot(sina_code: str, timeout: int = 15):
    """新浪取 ETF/指数 现货最新价（GBK）。返回 float 或 None。"""
    url = f"https://hq.sinajs.cn/list={sina_code}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            txt = r.read().decode("gbk", errors="replace")
    except Exception:
        return None
    m = re.search(r'"([^"]*)"', txt)
    if not m:
        return None
    parts = m.group(1).split(",")
    if len(parts) < 4:
        return None
    return _num(parts[3])        # 当前价（盘后=收盘价）


def _fetch_etf_options_sse(variety):
    """上交所 ETF 期权实时链（yunhq.sse.com.cn）。返回 [dict, ...]；无 bid/ask（免费口不提供）。"""
    idx = config.VARIETIES.get(variety, {}).get("index", "")
    underlying = idx[2:] if idx.startswith(("sh", "sz")) else idx
    if not underlying:
        return []

    from datetime import date, timedelta
    today = date.today()
    months_to_try = []
    for i in range(6):
        d = today + timedelta(days=32 * i)
        months_to_try.append(d.strftime("%m"))

    ops = []
    headers = {
        "User-Agent": config.CFFEX_HEADERS["User-Agent"],
        "Referer": "http://www.sse.com.cn/",
    }
    for mm in months_to_try:
        url = f"http://yunhq.sse.com.cn:32041/v1/sho/list/tstyle/{underlying}_{mm}"
        params = {"select": "contractid,last,chg_rate,presetpx,exepx"}
        full = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        try:
            req = urllib.request.Request(full, headers=headers)
            with _NO_PROXY_OPENER.open(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        rows = data.get("list", [])
        if not rows:
            continue
        for row in rows:
            if len(row) < 5:
                continue
            inst = str(row[0]).strip()
            m = _ETF_OPT_RE.match(inst)
            if not m:
                continue
            ops.append({
                "instrument": inst,
                "month": m.group(3),                  # 2610
                "cp": m.group(2).upper(),             # C / P
                "strike": _num(row[4]),               # exepx（元）
                "last": _num(row[1]),
                "bid": None,
                "ask": None,
                "volume": None,
                "position": None,
            })
    return ops


def _num(s):
    if isinstance(s, (int, float)):
        return float(s)
    s = (s or "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_options_raw(variety):
    url = config.CFFEX_QUOTE_URL.format(variety=variety)
    text = _fetch(url)
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = None
    for r in reader:
        if not r:
            continue
        if header is None:
            header = [c.strip() for c in r]
            continue
        if len(r) < 9:
            continue
        inst = parse_option_symbol(r[0])
        if not inst:
            continue
        rows.append({
            **inst,
            "instrument": r[0].strip(),
            # 注意列序：position, volume, lastprice, updown, bprice, bamount, sprice, samount
            "position": _num(r[1]) or 0,
            "volume": _num(r[2]) or 0,
            "last": _num(r[3]),
            "updown": _num(r[4]),
            "bid": _num(r[5]),
            "bid_vol": _num(r[6]) or 0,
            "ask": _num(r[7]),
            "ask_vol": _num(r[8]) or 0,
        })
    return rows


def fetch_options(variety="IO", use_cache=True):
    """返回该品种全部期权合约（所有到期月 + 所有行权价）"""
    variety = variety.upper()
    if use_cache:
        return _cached(f"opt:{variety}", lambda: _fetch_options_raw(variety))
    return _fetch_options_raw(variety)


def fetch_option_chain(variety="IO", month=None):
    """
    返回结构化期权链：
      {
        "variety": "IO",
        "months": ["2610","2611",...],
        "strikes": [4000,4050,...],
        "chain": { (month, cp, strike): {...row...} },
        "spot": 4433.5,               # 标的现价（股指=平价反推 / ETF=新浪现货）
        "source": "cffex" | "sse_yunhq" | "szse_unavailable",
      }
    """
    variety = variety.upper()
    if variety in ("E300", "E500", "E880"):
        ops = _fetch_etf_options_sse(variety)
        source = "sse_yunhq"
    elif variety == "E915":
        ops = []                          # SZSE 无免费逐合约实时源
        source = "szse_unavailable"
    else:
        ops = fetch_options(variety)
        source = "cffex"

    months = sorted({o["month"] for o in ops})
    if month:
        month = str(month)
        ops = [o for o in ops if o["month"] == month]
    strikes = sorted({o["strike"] for o in ops})

    chain = {}
    for o in ops:
        chain[(o["month"], o["cp"], o["strike"])] = o

    # 标的现价
    if variety in ("E300", "E500", "E880", "E915"):
        idx = config.VARIETIES.get(variety, {}).get("index", "")
        spot = _sina_spot(idx) if idx else None
        spot_samples = 1 if spot else 0
    else:
        # 股指：用平价关系反推 S = C - P + K（取中位数，抗噪）
        spots = []
        for (m, cp, k), o in chain.items():
            if cp != "C":
                continue
            p = chain.get((m, "P", k))
            if p and o.get("last") is not None and p.get("last") is not None:
                spots.append(o["last"] - p["last"] + k)
        spots.sort()
        spot = spots[len(spots) // 2] if spots else None
        spot_samples = len(spots)

    return {
        "variety": variety,
        "months": months,
        "strikes": strikes,
        "chain": chain,
        "spot": spot,
        "spot_samples": spot_samples,
        "count": len(ops),
        "source": source,
    }


# ------------------------------------------------------------------ 期货
_FUT_RE = re.compile(r"^([A-Z]{2})(\d{4})$")


def fetch_futures(variety="IF", use_cache=True):
    def _go():
        url = config.CFFEX_QUOTE_URL.format(variety=variety)
        text = _fetch(url)
        out = []
        for r in csv.reader(io.StringIO(text)):
            if not r or not _FUT_RE.match(r[0].strip()):
                continue
            if len(r) < 12:
                continue
            out.append({
                "instrument": r[0].strip(),
                "month": r[0].strip()[2:],
                "open": _num(r[1]), "high": _num(r[2]), "low": _num(r[3]),
                "last": _num(r[4]), "updown": _num(r[5]),
                "bid": _num(r[6]), "ask": _num(r[8]),
                "volume": _num(r[10]) or 0, "position": _num(r[11]) or 0,
            })
        return out
    if use_cache:
        return _cached(f"fut:{variety}", _go)
    return _go()


if __name__ == "__main__":
    import json
    chain = fetch_option_chain("IO")
    print("品种:", chain["variety"])
    print("到期月:", chain["months"])
    print("行权价数:", len(chain["strikes"]))
    print("合约数:", chain["count"])
    print("反推标的(沪深300):", round(chain["spot"], 2) if chain["spot"] else None,
          f"(基于 {chain['spot_samples']} 个行权价)")
    print()
    print("最近的 ATM 附近合约：")
    if chain["spot"]:
        atm_k = min(chain["strikes"], key=lambda k: abs(k - chain["spot"]))
        for m in chain["months"][:2]:
            for cp in ("C", "P"):
                o = chain["chain"].get((m, cp, atm_k))
                if o:
                    print(f"  {o['instrument']}  最新 {o['last']}  "
                          f"买 {o['bid']}x{o['bid_vol']}  卖 {o['ask']}x{o['ask_vol']}  "
                          f"持仓 {o['position']:.0f}")
    print()
    print("期货：")
    for f in fetch_futures("IF"):
        print(f"  {f['instrument']}  最新 {f['last']}  持仓 {f['position']:.0f}")
