# -*- coding: utf-8 -*-
"""
股指期货贴水数据抓取（直爬新浪版）

职责：
  1. 期货合约日线（股指 IC/IM/IF/IH + 铁矿石 i 的全部合约）→ SQLite futures_daily
  2. 现货指数日线（中证500/中证1000/沪深300/上证50）→ SQLite index_history
     （铁矿石没有新浪现货指数，本步跳过；现货另走生意社，见 docs/009 §1.1）
  3. 合约清单（上市日、最后交易日）→ SQLite contract_master

品种定义统一在 core/config.py 的 VARIETIES（本文件 import，不再各存一份）。

设计原则：
  - **不用 akshare**，直接请求新浪原始接口（与东哥其他程序一致，
    少一层依赖、少一个 "二道贩子" 的解析 bug 风险）。
  - 抓取与计算分离。本脚本只负责"把原始数据搬进库"。
  - **增量更新（两层，2026-10-04 优化）**：
      ① 合约级：只请求「最后交易日不早于 参照日 − GRACE_DAYS 天」的合约 ——
         已到期合约的日线已定型，再抓也不会变（实测请求数 600 → 40，耗时 2 分钟 → 18 秒）；
         **参照日 = min(今天, 库里最新日线日期)**，所以长期不开机也不会
         漏掉停机期间到期的合约（窗口跟着数据走，不跟日历走）；
      ② 日期级：写库时跳过库里已有的日期。
    想补齐历史或首次部署，用 --full。
  - 所有抓取都有异常兜底，单个合约失败不影响整体。

新浪接口（均已实测）：
  期货日线  https://stock.finance.sina.com.cn/futures/api/jsonp.php/
              var%20_x=/InnerFuturesNewService.getDailyKLine?symbol=IC2610
            → JSONP，字段 d/o/h/l/c/v(成交量)/p(持仓量)/s(结算价)
  指数日线  https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/
              CN_MarketData.getKLineData?symbol=sh000905&scale=240&ma=no&datalen=10000
            → 纯 JSON，字段 day/open/high/low/close/volume
  合约清单  中金所官方（akshare 已去，改由本地已知合约 + 规则推算，见下）

用法：
  python backend/fetch_data.py              # 增量更新（只抓未到期/刚到期合约的缺失日期）
  python backend/fetch_data.py --full       # 全量重拉（所有合约全历史，首次部署/补历史用）
  python backend/fetch_data.py --only=IC,IM # 只更新指定品种
  python backend/fetch_data.py --no-csv     # 不写 CSV 备份（默认也不写了）
"""

import os
import re
import sys
import json
import time
import urllib.request
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "core")
sys.path.insert(0, CORE)

import db  # noqa: E402
from config import VARIETIES  # noqa: E402  品种定义唯一真值来源在 core/config.py

MONTHS_ALL = list(range(1, 13))

HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

FUT_URL = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
           "var%20_x=/InnerFuturesNewService.getDailyKLine?symbol={}")
IDX_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol={}&scale=240&ma=no&datalen=10000")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def _f(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


# ============ 1. 期货日线（直爬新浪） ============
def fetch_futures_daily_raw(symbol):
    """直爬新浪期货日线。返回 list of dict，失败返回 None。

    接口返回 JSONP 包装：/*...*/ var _x=([{...},{...}]);
    需要剥掉外面的注释和 var 声明。
    """
    try:
        raw = _get(FUT_URL.format(symbol))
    except Exception:
        return None
    # 剥 JSONP：取第一个 '([' 到最后一个 '])'
    try:
        i = raw.index("([")
        j = raw.rindex("])")
        body = raw[i + 1:j + 1]     # 保留成 [ ... ]
        arr = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if not arr:
        return None
    rows = []
    for it in arr:
        d = (it.get("d") or "").strip()
        if not d:
            continue
        rows.append({
            "date": d[:10],
            "open": _f(it.get("o")),
            "high": _f(it.get("h")),
            "low": _f(it.get("l")),
            "close": _f(it.get("c")),
            "volume": _f(it.get("v")),
            "hold": _f(it.get("p")),
            "settle": _f(it.get("s")),
        })
    return rows or None


def last_date_in_db(symbol):
    """库里该合约已有的最新日期。"""
    with db.cursor(readonly=True) as con:
        r = con.execute("SELECT MAX(date) FROM futures_daily WHERE symbol=?",
                        (symbol,)).fetchone()
    return r[0] if r and r[0] else None


def save_futures(symbol, rows, incremental=True):
    """写库。incremental=True 时只写比库里更新的日期。

    返回 (写入行数, 是否成功)。
    """
    if not rows:
        return 0, False
    if incremental:
        last = last_date_in_db(symbol)
        if last:
            rows = [r for r in rows if r["date"] > last]
            if not rows:
                return 0, True
    data = [(r["date"], symbol, r["open"], r["high"], r["low"],
             r["close"], r["volume"], r["hold"], r["settle"]) for r in rows]
    db.upsert_futures(data)
    return len(data), True


# ============ 2. 指数日线（直爬新浪） ============
def fetch_index_daily_raw(code):
    """直爬新浪指数日线。code 形如 sh000905。返回 list of dict。"""
    try:
        raw = _get(IDX_URL.format(code))
    except Exception:
        return None
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not arr:
        return None
    rows = []
    for it in arr:
        d = (it.get("day") or "").strip()
        if not d:
            continue
        rows.append({"date": d[:10], "close": _f(it.get("close"))})
    return rows or None


def last_index_date_in_db(code):
    with db.cursor(readonly=True) as con:
        r = con.execute("SELECT MAX(date) FROM index_history WHERE symbol=?",
                        (code,)).fetchone()
    return r[0] if r and r[0] else None


def save_index(code, rows, incremental=True):
    if not rows:
        return 0, False
    if incremental:
        last = last_index_date_in_db(code)
        if last:
            rows = [r for r in rows if r["date"] > last]
            if not rows:
                return 0, True
    data = [(code, r["date"], r["close"]) for r in rows]
    db.upsert_index(data)
    return len(data), True


# ============ 3. 合约清单 ============
def parse_symbol(sym):
    """IC2610 / i2609 -> dict。品种代码**大小写都支持**（中金所大写、大商所小写）。"""
    m = re.match(r"([A-Za-z]+)(\d{2})(\d{2})$", sym)
    if not m:
        return None
    return {"variety": m.group(1), "year": 2000 + int(m.group(2)), "month": int(m.group(3))}


def third_friday(year, month):
    """交割月第三个周五 —— 中金所股指期货的最后交易日。"""
    d = datetime(year, month, 1)
    fridays = []
    while d.month == month:
        if d.weekday() == 4:
            fridays.append(d)
        d += timedelta(days=1)
    return fridays[2] if len(fridays) >= 3 else None


def nth_trading_day(year, month, n):
    """交割月第 n 个交易日（只跳周末，不含法定假日）。

    大商所铁矿石的最后交易日 = 交割月**第 10 个交易日**，不是中金所的第三个周五。
    没有节假日日历，按"跳周末"近似 —— 遇春节/国庆会差 1~3 天，但：
      · GRACE_DAYS=12 的宽限期能覆盖这点偏差（只用于"要不要抓"的筛选）；
      · 真正对外展示的剩余天数应该用**实际数据的最后一天**修正（后续步骤做）。
    """
    d = datetime(year, month, 1)
    cnt = 0
    while d.month == month:
        if d.weekday() < 5:
            cnt += 1
            if cnt == n:
                return d
        d += timedelta(days=1)
    return None


def last_trade_date(sym):
    """按交易所分派「最后交易日」规则。未知品种退回中金所规则（保守）。"""
    p = parse_symbol(sym)
    if not p:
        return None
    ex = VARIETIES.get(p["variety"], {}).get("exchange", "cffex")
    if ex == "dce":
        return nth_trading_day(p["year"], p["month"], 10)
    return third_friday(p["year"], p["month"])


def rebuild_contract_master():
    """重建合约清单。

    原版靠 akshare 的 futures_contract_info_cffex 拉官方清单，现已去掉。
    改为：从库里已有的合约（futures_daily）生成清单，
    最后交易日按**交易所规则**推算（中金所=第三个周五，大商所=第 10 个交易日）。
    """
    with db.cursor(readonly=True) as con:
        syms = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM futures_daily ORDER BY symbol")]

    rows = []
    for s in syms:
        p = parse_symbol(s)
        if not p:
            continue
        ltd = last_trade_date(s)
        rows.append((
            s, p["variety"], f"{p['year']}{p['month']:02d}", None, None,
            ltd.strftime("%Y-%m-%d") if ltd else None,
            None, None, None, None, None, datetime.now().strftime("%Y-%m-%d"),
        ))
    if rows:
        db.upsert_contracts(rows)
    return len(rows)


# ============ 候选合约枚举 ============
def gen_candidate_symbols(variety, start_year=None, end_year=None):
    cfg = VARIETIES[variety]
    sy = start_year or int(cfg["start"][:4])
    ey = end_year or (datetime.now().year + 1)
    out = []
    for y in range(sy, ey + 1):
        for m in MONTHS_ALL:
            out.append(f"{variety}{str(y)[2:]}{m:02d}")
    return out


# ============ 增量筛选：哪些合约还可能产生新数据？ ============
# 背景（2026-10-04 优化）：原来每次运行都对全部候选合约发请求（实测 600 次），
# 但其中绝大多数是早已到期、日线已定型的合约 —— 纯浪费，一次要跑 2 分钟。
#
# 判据很简单：**合约的最后交易日一旦过去，它的日线就永远不会再变**。
# 所以只需要抓「最后交易日不早于 参照日 − GRACE_DAYS 天」的合约：
#   - 未到期      → 抓（每天可能有新日线）
#   - 刚到期不久  → 抓（最后几天数据可能还没发布/没落库）
#   - 早已到期    → 跳过（数据已定型，再抓一万次也是同样的结果）
#
# ⚠️ 参照日必须取 **min(今天, 库里最新日线日期)**，不能直接用今天：
#   假设停机 30 天，期间有 3 个合约到期。若按"今天 − 12 天"筛，
#   这 3 个合约早已越过下界 → 被永久跳过 → 库里留下永远补不回来的空洞。
#   改用"库里最新日期 − 12 天"作下界，窗口就跟着数据走：只要数据没跟上，
#   窗口就不会往前滑，停机期间到期的合约仍然会被抓回来。
#
# GRACE_DAYS 取 12 天：覆盖国庆/春节最长 8~9 天的休市，再留一点余量，
# 也覆盖中金所"交割日遇节假日提前"导致的 last_trade_date 与第三个周五的偏差。
GRACE_DAYS = 12

# 还要设一个上界：合约挂得再远也还没挂牌，抓了是空响应。
#   中金所股指同时挂牌「当月、下月、当季、下季」，最远的季月约在 7~8 个月后；
#   大商所铁矿石挂满 12 个月份，最远约 13 个月后。
# 实际取值按品种从 config.VARIETIES[...]["months_ahead"] 读，下面是兜底默认值。
MONTHS_AHEAD = 8


def _db_latest_futures_date():
    """库里已入库的最新期货日线日期（ISO 字符串），空库返回 None。"""
    with db.cursor(readonly=True) as con:
        r = con.execute("SELECT MAX(date) FROM futures_daily").fetchone()
    return r[0] if r and r[0] else None


def _incremental_ref_date(today):
    """增量筛选的参照日 = min(今天, 库里最新日线日期)。

    见上方 ⚠️：取两者中较早的那个，保证"停机期间到期的合约"不会漏。
    空库或日期异常时退回今天。
    """
    latest = _db_latest_futures_date()
    if not latest:
        return today
    try:
        d = datetime.strptime(latest[:10], "%Y-%m-%d").date()
    except ValueError:
        return today
    return min(today, d)


def need_fetch(symbol, ref, today=None):
    """增量模式下判断该合约是否还需要发请求。

    ref   —— 下界参照日，见 _incremental_ref_date()：**跟着数据走**，
             保证停机期间到期的合约不会被漏掉。
    today —— 上界参照日，判断"是否已挂牌"应该按真实今天算（挂牌状态与
             库里数据新旧无关），默认与 ref 相同。
    """
    today = today or ref
    p = parse_symbol(symbol)
    if not p:
        return True                       # 解析不出来，保守抓
    ltd = last_trade_date(symbol)         # 按交易所分派（中金所第三个周五 / 大商所第 10 个交易日）
    if ltd is None:
        return True                       # 算不出到期日，保守抓
    ltd = ltd.date()

    # 下界：已到期且早于「参照日 − 宽限期」→ 日线已定型，跳过
    if ltd < ref - timedelta(days=GRACE_DAYS):
        return False

    # 上界：到期日还在 N 个月之外 → 尚未挂牌，跳过
    #   N 按品种取：中金所股指同时挂 4 个月份（最远下季，约 8 个月）；
    #   大商所铁矿石挂满 12 个月份 → 13（见 core/config.py 的 months_ahead）
    ahead = VARIETIES.get(p["variety"], {}).get("months_ahead", MONTHS_AHEAD)
    ey, em = today.year, today.month + ahead
    while em > 12:
        em -= 12
        ey += 1
    if (ltd.year, ltd.month) > (ey, em):
        return False

    return True


def filter_incremental_symbols(symbols, ref, today=None):
    """把候选合约过滤成"需要抓"的那一批。返回 (要抓的, 跳过的数量)。"""
    keep = [s for s in symbols if need_fetch(s, ref, today)]
    return keep, len(symbols) - len(keep)


# ============ 主流程 ============
def main():
    args = sys.argv[1:]
    full = "--full" in args
    incremental = not full
    only = None
    for a in args:
        if a.startswith("--only="):
            only = a.split("=", 1)[1].split(",")

    db.init_db()
    log("=" * 56)
    log(f"股指期货贴水数据 - {'全量' if full else '增量'}抓取（直爬新浪 → SQLite）")
    log(f"数据库: {db.DB_PATH}")
    log("=" * 56)

    varieties = only or list(VARIETIES.keys())

    # ---- [1/3] 现货指数 ----
    log("\n[1/3] 现货指数日线")
    for v in varieties:
        code = VARIETIES[v].get("spot_code")
        if not code:
            log(f"  – {v} ({VARIETIES[v]['name']}) 无新浪现货指数，跳过"
                f"（现货另走生意社 100ppi.com，见 docs/009 §1.1）")
            continue
        rows = fetch_index_daily_raw(code)
        if not rows:
            log(f"  ✗ {v} ({code}) 抓取失败")
            continue
        n, _ = save_index(code, rows, incremental=incremental)
        total = len(rows)
        log(f"  ✓ {v} ({code}): 接口 {total} 根，写入 {n} 根新增")

    # ---- [2/3] 期货合约 ----
    log("\n[2/3] 期货合约日线")
    today = datetime.now().date()
    ref = today if full else _incremental_ref_date(today)
    if incremental:
        log(f"  增量筛选参照日 {ref}（宽限 {GRACE_DAYS} 天，上界 +{MONTHS_AHEAD} 个月）")
    grand_new = 0
    for v in varieties:
        all_syms = gen_candidate_symbols(v)
        if incremental:
            syms, skipped = filter_incremental_symbols(all_syms, ref, today)
        else:
            syms, skipped = all_syms, 0
        ok, new_rows, tried = 0, 0, 0
        for s in syms:
            tried += 1
            rows = fetch_futures_daily_raw(s)
            if not rows:
                continue
            n, success = save_futures(s, rows, incremental=incremental)
            if success:
                ok += 1
                new_rows += n
        grand_new += new_rows
        extra = f"，跳过 {skipped} 个已到期合约" if skipped else ""
        log(f"  {v}: 有效合约 {ok} / 请求 {tried}{extra}，新增 {new_rows} 行")

    # ---- [3/3] 合约清单 ----
    log("\n[3/3] 合约清单")
    n_c = rebuild_contract_master()
    log(f"  ✓ 合约清单 {n_c} 条")

    log("\n完成。数据库统计：")
    for k, val in db.stats().items():
        log(f"    {k}: {val}")
    log(f"本次新写入期货日线 {grand_new} 行")


if __name__ == "__main__":
    main()
