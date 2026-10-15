# -*- coding: utf-8 -*-
"""
深交所 ETF 期权**官方**日行情直爬（Optarb Phase 2a：创业板ETF 159915 → E915）

数据源（东哥要求：不用"二道贩子"，直爬官方）：
    https://www.szse.cn/api/report/ShowReport/data
        ?SHOWTYPE=JSON&CATALOGID=option_drhy&TABKEY=tab1&PAGENO=N

★ URL 定位（2026-09-29，避免以后再搜）：AKShare `akshare/option/option_current_szse.py`
  用 `CATALOGID=option_drhy`（它写成 www.sse.org.cn 是笔误，真域名 www.szse.cn，实测通）。
  AKShare 另两个 SZSE 报告 `ysplbrb`(合约信息无价) / `ysprdzb`(日度概况汇总) 都不含逐合约价，
  只有 `option_drhy`（当日合约）可作为逐合约源。

★ 字段（JSON list[{metadata, data, error}]，data 每页 20 条，需翻页）：
    hydm     159915C2612M003400   合约代码（标6位+CP+YYMM+M+行权价×1000，**优先解析**）
    hyjc     创业板ETF购12月3400  合约简称
    bdzqdm   创业板ETF易方达(159915)  标的（含 6 位代码，交叉确认）
    hylx     认购 / 认沽           CP（中文！不是 C/P 字母）
    xqjg     3.4000               行权价（元）
    hydw     10000               合约单位=份/张
    hzjyrq   2026-12-23          最后交易日（第4周三）
    qjsjg    0.1065              **前结算价**
    ztjg/dtjg 涨停/跌停价

★ 两个硬限制（务必知道，详见 docs/008 §四）：
    1. 该报告**只有「前结算价」qjsjg，没有当日收盘价/结算价**。
       → 移仓价差用 qjsjg 当 close 与 settle（同 SSE 的 close==settle 口径）；
         近月/远月都用同日「前结算价」，价差 far−near 仍有效（迁移看价差不看绝对价）。
    2. **没有 ETF 现货字段** → 现货从**新浪** `hq.sinajs.cn/list=sz159915`（GBK+Referer）取，
       存入 index_daily。东哥接受新浪作直接行情源（future/arb 也用）。

★ 解析：hydm r'^(\d{6})([CP])(\d{4})M(\d+)$' → underlying / cp / expiry(YYMM) / strike(×1000→元)
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date

from core import config

# ★ 与项目其他模块一致：强制直连，不继承环境代理
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE_HOST = "www.szse.cn"
BASE_PATH = "/api/report/ShowReport/data"

# ★ 2026-10-01 实测：https 会被服务端直接断连（RemoteDisconnected），http 稳定可用。
#   两个都试，http 优先 —— 否则每天抓取都失败，E915 历史永远累积不起来。
SCHEMES = ("http", "https")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Host": BASE_HOST,
}

# hydm = 159915C2612M003400
#   group(1)=159915（标的6位）  group(2)=C/P  group(3)=2612（YYMM）  group(4)=003400（行权价×1000）
_CONTRACT_RE = re.compile(r"^(\d{6})([CP])(\d{4})M(\d+)$", re.IGNORECASE)

# 深交所 ETF 期权标的 -> 品种（本期只上 159915）
ETF_UNDERLYING_TO_VARIETY = {
    "159915": "E915",
}

# 新浪 ETF 现货代码（用于取标的现价；SZSE ETF 用 sz 前缀）
SPOT_SINA = {
    "159915": "sz159915",
}


def _f(v):
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "null", "None", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _sina_spot(sina_code: str, timeout: int = 15):
    """从新浪取 ETF 现货最新价（GBK）。返回 float 或 None。"""
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
    # var hq_str_sz159915="创业板,今开,昨收,当前,最高,最低,..."
    m = re.search(r'"([^"]*)"', txt)
    if not m:
        return None
    parts = m.group(1).split(",")
    if len(parts) < 4:
        return None
    return _f(parts[3])        # 当前价（盘后=收盘价）


def fetch_raw_page(pageno: int, timeout: int = 25, retries: int = 2):
    """抓单页 JSON（list[{metadata,data,error}]）。返回该结构或 None。

    ★ 逐个 scheme 试（http 优先）：https 会被服务端断连（见 SCHEMES 注释）。
    """
    last_err = None
    for scheme in SCHEMES:
        url = (f"{scheme}://{BASE_HOST}{BASE_PATH}"
               f"?SHOWTYPE=JSON&CATALOGID=option_drhy&TABKEY=tab1&PAGENO={pageno}")
        headers = dict(HEADERS)
        headers["Referer"] = f"{scheme}://{BASE_HOST}/"
        for i in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
                    return r.read().decode("utf-8", errors="replace")
            except Exception as e:  # noqa: BLE001
                last_err = e
                if i < retries:
                    time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"抓取深交所期权第{pageno}页失败: {last_err}")


def _find_block(text):
    """从 JSON 文本取含 data 的 block + trade_date。"""
    if not text:
        return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, list):
        return None, None
    block = next((b for b in data if isinstance(b, dict) and b.get("data")), None)
    trade_date = None
    if block:
        sub = (block.get("metadata") or {}).get("subname")
        if sub:
            trade_date = sub[:10] if len(sub) >= 10 else sub
    return block, trade_date


def parse_blocks(blocks, trade_date):
    """
    blocks: list of raw row dict（已拼接所有页的 data）
    返回 {trade_date, options, spots}
    """
    options = []
    spots = {}
    for r in blocks:
        hydm = (r.get("hydm") or "").strip()
        m = _CONTRACT_RE.match(hydm)
        if not m:
            continue
        underlying = m.group(1)                       # 159915
        cp_letter = m.group(2).upper()                # C / P
        expiry = m.group(3)                           # 2612
        strike = int(m.group(4)) / 1000.0            # 3.400（元）

        variety = ETF_UNDERLYING_TO_VARIETY.get(underlying)
        if not variety:
            continue                                 # 只上 159915

        # 中文 CP 交叉确认
        hylx = (r.get("hylx") or "").strip()
        cp_cn = "C" if "认购买" in hylx or hylx == "认购" else ("P" if "沽" in hylx else None)
        if cp_cn and cp_cn != cp_letter:
            cp = cp_cn
        else:
            cp = cp_letter

        px = _f(r.get("qjsjg"))                        # 前结算价（当 close/settle 用）
        if px is None:
            continue

        options.append({
            "symbol": hydm,                           # 159915C2612M003400（PK 唯一）
            "variety": variety,
            "expiry": expiry,
            "cp": cp,
            "strike": strike,
            "open": None, "high": None, "low": None,
            "close": px,
            "settle": px,                             # SZSE 报告无当日结算价，用前结算价
            "presettle": px,
            "volume": None,                           # ★ 该报告无成交量字段
            "amount": None,
            "oi": None,                               # ★ 该报告无持仓量字段
            "oi_chg": None,
            "delta": None,
        })

        # ETF 现货（159915）→ 新浪 sz159915
        if underlying in SPOT_SINA and "sz159915" not in spots:
            sp = _sina_spot(SPOT_SINA[underlying])
            if sp is not None:
                spots["sz159915"] = sp

    if not trade_date:
        trade_date = date.today().isoformat()
    return {"trade_date": trade_date, "options": options, "spots": spots}


def fetch_day(timeout: int = 25, retries: int = 2):
    """抓「最新交易日」的深交所 ETF 期权快照（仅 E915）。无数据返回 None。"""
    all_blocks = []
    trade_date = None
    page = 1
    while True:
        text = fetch_raw_page(page, timeout=timeout, retries=retries)
        block, td = _find_block(text)
        if not block or not block.get("data"):
            break
        all_blocks.extend(block["data"])
        if td and not trade_date:
            trade_date = td
        meta = block.get("metadata") or {}
        pagecount = meta.get("pagecount")
        if pagecount and page >= int(pagecount):
            break
        if len(block["data"]) < 20:
            break
        page += 1
        time.sleep(0.2)

    if not all_blocks:
        return None
    return parse_blocks(all_blocks, trade_date)


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
        print("\nE915 样例（前 4 条）:")
        for o in [x for x in d["options"] if x["variety"] == "E915"][:4]:
            pprint.pprint(o)
