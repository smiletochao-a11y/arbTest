# -*- coding: utf-8 -*-
"""
实时行情模块：直接请求新浪期货/指数行情接口。

背景：AkShare 的 futures_zh_spot 在 1.18.x 版本有解析 bug
     （ValueError: Length mismatch: Expected axis has 50 elements），
     故绕过 AkShare，直接解析新浪原始接口。

接口：https://hq.sinajs.cn/list=nf_IC2610,nf_IC2611,sh000905
要求：必须带 Referer: https://finance.sina.com.cn，否则被拒。

期货字段位（实测确认，对照日线昨收/持仓量验证）：
  0  开盘        1  最高        2  最低        3  当前价
  4  成交量(手)  5  成交额      6  持仓量      7  空
  8  空          9  涨停       10  跌停       11-12 空
  13 昨收       14 昨结       15 昨日持仓    16 昨收(备用)
  ...
  36 日期       37 时间       49 中文名

现货指数字段位（sh000905）：
  0 名称  1 今开  2 昨收  3 当前价  4 最高  5 最低
  ... 30 日期  31 时间
"""
import urllib.request
from datetime import datetime

SINA_URL = "https://hq.sinajs.cn/list={}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


def _fetch_raw(codes):
    """批量拉取原始行情字符串。返回 {code: [fields]}。"""
    url = SINA_URL.format(",".join(codes))
    req = urllib.request.Request(url, headers=HEADERS)
    raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
    out = {}
    for line in raw.strip().split("\n"):
        if "=" not in line or '"' not in line:
            continue
        try:
            head = line.split("=")[0].strip()          # var hq_str_nf_IC2610
            code = head.replace("var hq_str_", "")
            body = line.split('"')[1]
            if not body:
                continue
            out[code] = body.split(",")
        except Exception:
            continue
    return out


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def fetch_futures_quote(symbols):
    """
    拉取期货实时行情。
    symbols: ['IC2610', 'IC2611', 'i2701', ...]
    返回 list of dict:
      {symbol, price, pre_close, open, high, low, volume, hold,
       change, change_pct, date, time}

    ⚠️ 新浪实时接口（hq.sinajs.cn）**只认大写品种代码**：
       请求 nf_I2701 → 有数据；nf_i2701 → 返回空 body。
    （日线接口 getDailyKLine 大小写都认，只有实时接口有这个坑，见 docs/009 差异 3）
    所以这里**只把请求代码转大写**，返回的 symbol 仍是调用方传进来的原样，
    上层（如 /api/move 的 live_prices）用原代码做 key，不用感知大小写。
    """
    if not symbols:
        return []
    codes = ["nf_" + s.upper() for s in symbols]
    data = _fetch_raw(codes)
    out = []
    for s in symbols:
        f = data.get("nf_" + s.upper())
        if not f or len(f) < 38:
            continue
        price = _f(f[3])
        pre_close = _f(f[13]) or _f(f[16])
        change = None
        change_pct = None
        if price is not None and pre_close:
            change = price - pre_close
            change_pct = change / pre_close * 100
        out.append({
            "symbol": s,
            "price": price,
            "pre_close": pre_close,
            "open": _f(f[0]),
            "high": _f(f[1]),
            "low": _f(f[2]),
            "volume": _f(f[4]),
            "hold": _f(f[6]),
            "change": round(change, 1) if change is not None else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "date": f[36] if len(f) > 36 else None,
            "time": f[37] if len(f) > 37 else None,
        })
    return out


def fetch_index_quote(spot_code):
    """
    拉取现货指数实时行情。spot_code 形如 'sh000905'。
    返回 dict: {name, price, pre_close, open, high, low, change, change_pct, date, time}

    没有现货代码的品种（如铁矿石）直接返回 None —— 不能把 None 拼进请求 URL，
    否则 ", ".join 会抛 TypeError，整个批量接口 500。
    """
    if not spot_code:
        return None
    data = _fetch_raw([spot_code])
    f = data.get(spot_code)
    if not f or len(f) < 6:
        return None
    name = f[0]
    price = _f(f[3])
    pre_close = _f(f[2])
    change = None
    change_pct = None
    if price is not None and pre_close:
        change = price - pre_close
        change_pct = change / pre_close * 100
    return {
        "name": name,
        "price": price,
        "pre_close": pre_close,
        "open": _f(f[1]),
        "high": _f(f[4]),
        "low": _f(f[5]),
        "change": round(change, 2) if change is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "date": f[30] if len(f) > 30 else None,
        "time": f[31] if len(f) > 31 else None,
    }


def is_trading_time():
    """粗略判断是否在交易时段（用于前端提示）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)
