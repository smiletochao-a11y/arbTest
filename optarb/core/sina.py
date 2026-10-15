# -*- coding: utf-8 -*-
"""
新浪指数日线 —— 只用来拿「标的指数收盘价」

用途：
  1. 判断期权是实值还是虚值（K vs 现货收盘）
  2. 算「价差/现货」（%）

注：股指期权本身中金所不给指数收盘，必须外部取。
"""
import json
import re
import threading
import time
import urllib.request

from core import market_calendar

# ★ 强制直连，不继承环境代理（理由见 core/quotes.py 的注释）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★ 2026-10-03：实时快照加进程内缓存。
#   原来每次 /api/live 都打一次新浪 —— 周末/假期/午休时指数根本不动，纯属浪费。
#   TTL 走 market_calendar.quote_ttl()：盘中 30 秒，冻结期间直接设到下次可能变化。
_RT_CACHE = {}
_RT_LOCK = threading.Lock()

URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
       "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}")

# ★ 实时快照（2026-09-28 新增）
#   日线接口只到"上一交易日收盘"，实时页拿它当 F 会滞后一整天
#   —— 大跌当天还在用昨天的现货定平值（9-28 中证1000 7294，程序用 9-24 的 7570）。
RT_URL = "https://hq.sinajs.cn/list={symbol}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    # ★ 必须带 Referer，否则 hq.sinajs.cn 返回 403
    "Referer": "https://finance.sina.com.cn",
}


def _loose_json(text: str):
    """新浪有时返回 JS 对象字面量（key 无引号），做一次兼容"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r'([{,])\s*([A-Za-z_]\w*)\s*:', r'\1"\2":', text)
        return json.loads(fixed)


def fetch_index_daily(symbol: str, n: int = 1000, timeout: int = 20):
    """返回 [{'day': '2026-09-24', 'close': 4466.0}, ...]（升序）"""
    url = URL.format(symbol=symbol, n=n)
    req = urllib.request.Request(url, headers=HEADERS)
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    data = _loose_json(raw) or []
    out = []
    for it in data:
        try:
            out.append({"day": it["day"][:10], "close": float(it["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["day"])
    return out


def fetch_index_close_map(symbol: str, n: int = 1000):
    """{'2026-09-24': 4466.0, ...}"""
    return {r["day"]: r["close"] for r in fetch_index_daily(symbol, n)}


def fetch_index_realtime(symbol: str, timeout: int = 10):
    """
    新浪实时指数快照（盘中用）。

    返回 dict 或 None：
      {name, open, prev_close, price, high, low, date, time}
      date/time = 该快照对应的行情日期与时间（★ 页面必须显示，用来判断数据新鲜度）

    实测（2026-09-28）：
      var hq_str_sh000852="中证1000,7551.2173,7570.1448,7294.1735,7551.2173,7246.6120,...,2026-09-28,15:30:36,...";
        f[0]=名称 f[1]=今开 f[2]=昨收 f[3]=当前 f[4]=最高 f[5]=最低 ... f[30]=日期 f[31]=时间

    ⚠️ 编码 GBK；必须带 Referer（否则 403）。

    ★ 2026-10-03：加进程内缓存（TTL 按市场状态动态算）。失败不缓存，下次会重试。
    """
    now = time.time()
    ttl = market_calendar.quote_ttl()
    with _RT_LOCK:
        hit = _RT_CACHE.get(symbol)
        if hit and now - hit[0] < ttl:
            return hit[1]

    url = RT_URL.format(symbol=symbol)
    req = urllib.request.Request(url, headers=HEADERS)
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode("gbk", errors="replace")
    m = re.search(r'="([^"]*)"', raw)
    if not m or not m.group(1):
        return None
    f = m.group(1).split(",")
    if len(f) < 32:
        return None

    def _f(i):
        try:
            return float(f[i])
        except (ValueError, IndexError):
            return None

    price = _f(3)
    if price is None or price <= 0:
        return None
    out = {
        "symbol": symbol,
        "name": f[0],
        "open": _f(1),
        "prev_close": _f(2),
        "price": price,
        "high": _f(4),
        "low": _f(5),
        "date": f[30],
        "time": f[31],
    }
    with _RT_LOCK:
        _RT_CACHE[symbol] = (now, out)
    return out
