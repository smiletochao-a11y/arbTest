# -*- coding: utf-8 -*-
"""
上交所 ETF 期权**官方**日行情直爬（Optarb Phase 1：300ETF / 500ETF）

数据源（东哥要求：不用"二道贩子"，直爬官方）：
    http://query.sse.com.cn/commonQuery.do?isPagination=false
        &sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L

★ 实测可用（2026-09-28 探测）：
    - 返回当日**全部** ETF 期权合约（含 50ETF/300ETF/500ETF，共 580 个）
    - 字段（JSON `result` 数组）：
        CONTRACT_ID       510300C2610M04000  （标6位 + C/P + YYMM + M + 行权价×1000）
        CONTRACT_SYMBOL   300ETF购10月4000
        CALL_OR_PUT       认购 / 认沽
        EXERCISE_PRICE    4.000                （元）
        SECURITY_CLOSEPX  0.4267              （期权收盘价，元/份）
        SETTL_PRICE       0.4267              （结算价）
        EXPIRE_DATE       20261028            （第4周三，与规则一致）
        CONTRACT_UNIT     10000               （合约单位 = 份/张）
        SECURITYNAMEBYID  300ETF(510300)      （标的 ETF 代码）
        UNDERLYING_CLOSEPX 4.417              ★ ETF 现货价（直接可得，免得再爬别的源）
        TIMESAVE          2026-09-28          （数据日期）

★ 限制（已知，Phase 1 接受）：
    1. 该端点**只返回最新交易日**，不支持历史回溯（tradeDate 参数无效）。
       → ETF 历史深度只能靠"每天收盘后增量抓"累积；当日快照含多个到期月，
         足以算"跨月移仓价差"。
    2. **没有成交量/持仓量**字段 → opt_daily.volume/oi 存 None。
       migrate 的 `_px` 对 None 成交量取结算价（此处 close==settle，等价）；
       `zero_vol` 不再因 None 灰行（见 migrate.build_table）。

★ 解析策略：优先用 CONTRACT_ID（格式最规整、含全部维度），
  SECURITYNAMEBYID 仅用于交叉确认标的代码。
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from core import config

# ★ 与项目其他模块一致：强制直连，不继承环境代理（理由见 core/quotes.py）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE = ("http://query.sse.com.cn/commonQuery.do?isPagination=false"
        "&sqlId=SSE_ZQPZ_YSP_GGQQZSXT_XXPL_DRHY_SEARCH_L")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "http://www.sse.com.cn/",
    "Host": "query.sse.com.cn",
}

# CONTRACT_ID = 510300C2610M04000
#   group(1) = 510300（标的 6 位代码）
#   group(2) = C / P
#   group(3) = 2610（YYMM）
#   group(4) = 04000（行权价 ×1000）
_CONTRACT_RE = re.compile(r"^(\d{6})([CP])(\d{4})M(\d+)$", re.IGNORECASE)


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


def fetch_raw(timeout: int = 20, retries: int = 2):
    """抓原始 JSON 文本。返回 None = 无数据（非交易日/接口异常）。"""
    last_err = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(BASE, headers=HEADERS)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries:
                time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"抓取上交所 ETF 期权失败: {last_err}")


def parse(raw: str):
    """
    解析为结构化结果：
      {
        "trade_date": "2026-09-28",
        "options": [  # opt_daily 同构的规范化 dict（只含 E300/E500）
          {symbol, variety, expiry, cp, strike, open, high, low,
           close, settle, presettle, volume, amount, oi, oi_chg, delta},
          ...
        ],
        "spots": { "sh510300": 4.417, "sh510500": ... },  # ETF 现货（UNDERLYING_CLOSEPX）
      }
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    rows = data.get("result") if isinstance(data, dict) else None
    if not rows:
        return None

    trade_date = None
    options = []
    spots = {}
    for r in rows:
        cid = (r.get("CONTRACT_ID") or "").strip()
        m = _CONTRACT_RE.match(cid)
        if not m:
            continue
        underlying = m.group(1)                      # 510300
        cp = m.group(2).upper()                      # C / P
        expiry = m.group(3)                          # 2610
        strike = int(m.group(4)) / 1000.0           # 4.000（元）

        variety = config.ETF_UNDERLYING_TO_VARIETY.get(underlying)
        if not variety:
            continue                                 # Phase 1 只上 510300/510500

        close = _f(r.get("SECURITY_CLOSEPX"))
        settle = _f(r.get("SETTL_PRICE"))
        if close is None and settle is None:
            continue                                 # 无价合约跳过

        # 数据日期：优先 CONTRACT 的 TIMESAVE，统一成 YYYY-MM-DD
        ts = (r.get("TIMESAVE") or "").strip()
        if len(ts) >= 10:
            trade_date = ts[:10]

        # ETF 现货（同一品种多个合约共享，取最后一个即可，值相同）
        uc = _f(r.get("UNDERLYING_CLOSEPX"))
        if uc is not None:
            sym = config.VARIETIES[variety]["index"]
            spots[sym] = uc

        options.append({
            "symbol": cid,                  # 510300C2610M04000（PK 唯一）
            "variety": variety,
            "expiry": expiry,
            "cp": cp,
            "strike": strike,
            "open": None, "high": None, "low": None,
            "close": close,
            "settle": settle,
            "presettle": None,
            "volume": None,                # ★ 该端点无成交量字段
            "amount": None,
            "oi": None,                    # ★ 该端点无持仓量字段
            "oi_chg": None,
            "delta": None,
        })

    if not trade_date:
        trade_date = date.today().isoformat()
    return {"trade_date": trade_date, "options": options, "spots": spots}


def fetch_day(timeout: int = 20, retries: int = 2):
    """抓「最新交易日」的 ETF 期权快照（E300/E500）。无数据返回 None。"""
    raw = fetch_raw(timeout=timeout, retries=retries)
    return parse(raw)


if __name__ == "__main__":
    import pprint
    d = fetch_day()
    if not d:
        print("无数据")
    else:
        print("数据日期:", d["trade_date"])
        print("ETF 现货:", d["spots"])
        byv = {}
        for o in d["options"]:
            byv.setdefault(o["variety"], 0)
            byv[o["variety"]] += 1
        print("各品种合约数:", byv)
        print("\nE300 样例（前 4 条）:")
        for o in [x for x in d["options"] if x["variety"] == "E300"][:4]:
            pprint.pprint(o)
