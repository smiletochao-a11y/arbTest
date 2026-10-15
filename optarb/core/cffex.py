# -*- coding: utf-8 -*-
"""
中金所历史日行情：抓取 + 解析

★ 路径（2026-09-27 实测可用）
    http://www.cffex.com.cn/sj/hqsj/rtj/{YYYYMM}/{DD}/{YYYYMMDD}_1.csv
    - 编码 GBK（不是 UTF-8）
    - _1.csv 一个文件含：股指期货 + 股指期权 + 国债期货
    - 需要 UA；Referer 带上更稳
    - 回溯：至少到 2015；IO 期权自 2019-12-23 上市

★ 字段（14 列）
    合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,
    今收盘,今结算,前结算,涨跌1,涨跌2,Delta
    → 有收盘/结算/Delta；**没有 IV**

★ 踩坑
    - 404 时返回 302 跳 error_404.html（HTTP 状态不是 404）→ 必须校验内容首行
    - 无成交合约「今收盘」为 null，但「今结算」有值 → 这就是「零成交用结算价」口径的来源
"""
import csv
import io
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# ★ 强制直连，不继承环境代理（理由见 core/quotes.py 的注释）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE = "http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/{date}_1.csv"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "http://www.cffex.com.cn/",
}

# 期权合约：IO2610-C-4000 / MO2610-P-8500
OPT_RE = re.compile(r"^([A-Z]{2})(\d{4})-([CP])-(\d+(?:\.\d+)?)$")
# 期货合约：IF2610
FUT_RE = re.compile(r"^([A-Z]{2})(\d{4})$")

OPTION_VARIETIES = ("IO", "HO", "MO")
FUTURE_VARIETIES = ("IF", "IH", "IC", "IM")

# 期权品种 -> 标的指数（新浪代码）
INDEX_OF = {"IO": "sh000300", "HO": "sh000016", "MO": "sh000852"}


def _url(date_str: str) -> str:
    return BASE.format(ym=date_str[:6], dd=date_str[6:8], date=date_str)


def fetch_raw(date_str: str, timeout: int = 20, retries: int = 2) -> bytes:
    """按日期抓原始字节（GBK）。返回 b'' 表示该日无数据（非交易日）。"""
    last_err = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(_url(date_str), headers=HEADERS)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
                raw = r.read()
            text = raw.decode("gbk", errors="replace")
            if "合约代码" not in text[:200]:
                return b""          # 被重定向到错误页
            return raw
        except Exception as e:      # noqa: BLE001
            last_err = e
            if i < retries:
                time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"抓取 {date_str} 失败: {last_err}")


def parse(raw: bytes):
    """GBK 解码 + 解析为 list[dict]（保留原始中文列名）"""
    if not raw:
        return []
    text = raw.decode("gbk", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]


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


def _i(v):
    f = _f(v)
    return None if f is None else int(f)


def parse_option_symbol(sym: str):
    """IO2610-C-4000 -> dict(variety, expiry, cp, strike)"""
    m = OPT_RE.match(sym.strip())
    if not m:
        return None
    return {"variety": m.group(1), "expiry": m.group(2),
            "cp": m.group(3), "strike": float(m.group(4))}


def parse_future_symbol(sym: str):
    """IF2610 -> dict(variety, expiry)"""
    m = FUT_RE.match(sym.strip())
    if not m or m.group(1) not in FUTURE_VARIETIES:
        return None
    return {"variety": m.group(1), "expiry": m.group(2)}


def classify(rows):
    """
    把原始行拆成 (options, futures)，每项是**规范化后**的 dict。

    期权 dict: date/symbol/variety/expiry/cp/strike/open/high/low/close/settle/
               presettle/volume/amount/oi/oi_chg/delta
    期货 dict: date/symbol/variety/expiry/open/high/low/close/settle/
               volume/amount/oi/oi_chg
    """
    options, futures = [], []
    for r in rows:
        vals = list(r.values())
        sym = (vals[0] or "").strip()
        if not sym or "小计" in sym or "合计" in sym:
            continue
        common = {
            "symbol": sym,
            "open": _f(r.get("今开盘")), "high": _f(r.get("最高价")),
            "low": _f(r.get("最低价")), "close": _f(r.get("今收盘")),
            "settle": _f(r.get("今结算")), "presettle": _f(r.get("前结算")),
            "volume": _i(r.get("成交量")), "amount": _f(r.get("成交金额")),
            "oi": _i(r.get("持仓量")), "oi_chg": _i(r.get("持仓变化")),
        }
        o = parse_option_symbol(sym)
        if o:
            common.update(o)
            common["delta"] = _f(r.get("Delta"))
            options.append(common)
            continue
        fu = parse_future_symbol(sym)
        if fu:
            common.update(fu)
            futures.append(common)
    return options, futures


def is_trade_day(date_str: str) -> bool:
    """粗略判断：工作日 + 中金所有数据"""
    d = datetime.strptime(date_str, "%Y%m%d")
    if d.weekday() >= 5:
        return False
    try:
        return bool(fetch_raw(date_str))
    except Exception:  # noqa: BLE001
        return False


def last_trade_day(from_date=None, lookback: int = 15):
    """从 from_date（默认今天）往前找最近有数据的交易日，返回 'YYYYMMDD' 或 None"""
    d = from_date or date.today()
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y%m%d").date()
    for i in range(lookback):
        s = (d - timedelta(days=i)).strftime("%Y%m%d")
        if datetime.strptime(s, "%Y%m%d").weekday() >= 5:
            continue
        try:
            if fetch_raw(s):
                return s
        except Exception:  # noqa: BLE001
            continue
    return None
