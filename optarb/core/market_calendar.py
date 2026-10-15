# -*- coding: utf-8 -*-
"""交易日历 / 市场状态 —— 回答「现在有没有实时行情」。

★ 2026-10-03 新增（东哥要求）：
  周末、法定假期、中午休市的时候**根本没有实时行情**，
  不该按 30 秒一次的节奏反复去爬中金所的实时期权链 —— 那是纯浪费。

本模块只做两件事：
  1. `market_state()` —— 现在处于什么状态（盘中 / 午休 / 盘前 / 收盘后 / 休市）
  2. `quote_ttl()`    —— 行情缓存该设多长（**状态会变时才短，冻结期间直接设到下次变化**）

设计要点（**别改错**）：
  · 缓存 TTL 是**唯一的**「要不要再去爬」的闸门 —— 所有行情抓取都走
    `quotes._cached()`，改那里的默认 TTL 就等于全系统生效。
  · 冻结期间 TTL 设成「到下一次可能变化的那一刻」，所以整个周末只爬 **1 次**，
    而不是每 30 秒爬一次。
  · 判据一律写成「日期在未来 / 时间在区间内」，**不写死具体日期**，
    否则每年假期都要改代码。

⚠️ 休市日来源 = `config.HOLIDAYS_FUTURE`（人工维护的未来公告表）。
   它只覆盖「已公告但还没有行情」的那一段，够用（见 config 里那段注释）。
   周末永远休市（调休上班日交易所也不开），所以不用查表。
"""
from datetime import date, datetime, time as dtime, timedelta

from core import config

# ---- 交易时段（中金所股指期货 / 上交所 ETF 期权一致）------------------------
#   09:15 集合竞价开始 → 09:30 连续竞价 → 11:30 上午收
#   13:00 下午开 → 15:00 收
AUCTION_START = dtime(9, 15)     # 集合竞价开始（价格已开始变动）
AM_OPEN = dtime(9, 30)
AM_CLOSE = dtime(11, 30)
PM_OPEN = dtime(13, 0)
PM_CLOSE = dtime(15, 0)

# 收盘后「等收盘价落定」的缓冲：这段时间仍用短 TTL，之后才冻结
SETTLE_UNTIL = dtime(15, 15)
SETTLE_TTL = 120

STATE_LABEL = {
    "open": "盘中",
    "lunch": "午间休市",
    "pre": "盘前",
    "post": "已收盘",
    "closed": "休市",
}


def _now(now=None):
    return now or datetime.now()


def is_holiday(d):
    """d 是否在 `config.HOLIDAYS_FUTURE` 里（法定休市日）。"""
    for days in (config.HOLIDAYS_FUTURE or {}).values():
        if d.isoformat() in days:
            return True
    return False


def is_trading_day(d):
    """周末永远不是交易日（调休上班日交易所也不开）；工作日再排法定假期。"""
    return d.weekday() < 5 and not is_holiday(d)


def market_state(now=None):
    """返回 'open' | 'lunch' | 'pre' | 'post' | 'closed'。"""
    n = _now(now)
    if not is_trading_day(n.date()):
        return "closed"
    t = n.time()
    if t < AUCTION_START:
        return "pre"
    if t < AM_CLOSE:          # 09:15~11:30（含集合竞价）
        return "open"
    if t < PM_OPEN:           # 11:30~13:00
        return "lunch"
    if t < PM_CLOSE:          # 13:00~15:00
        return "open"
    return "post"             # 15:00 之后


def next_session_start(now=None):
    """下一次「可能开始有新行情」的时刻 —— 下一个交易日的 09:15。

    今天还没到 09:15 就是今天 09:15；否则往后找下一个交易日。
    """
    n = _now(now)
    d = n.date()
    if is_trading_day(d) and n.time() < AUCTION_START:
        return datetime.combine(d, AUCTION_START)
    for _ in range(30):        # 最长连续假期不会超过 30 天
        d += timedelta(days=1)
        if is_trading_day(d):
            return datetime.combine(d, AUCTION_START)
    return datetime.combine(d, AUCTION_START)   # 兜底


def quote_ttl(now=None):
    """行情缓存秒数。

    · 盘中（含集合竞价）→ 用 `config.QUOTE_CACHE_SEC`（默认 30 秒，真·实时）
    · 其余状态          → 直接设到「下一次可能变化」的那一刻，整段冻结只爬一次
    """
    n = _now(now)
    st = market_state(n)
    if st == "open":
        return config.QUOTE_CACHE_SEC
    if st == "lunch":
        return max(1, int((datetime.combine(n.date(), PM_OPEN) - n).total_seconds()))
    if st == "pre":
        return max(1, int((datetime.combine(n.date(), AUCTION_START) - n).total_seconds()))
    if st == "post" and n.time() < SETTLE_UNTIL:
        # 15:00~15:15：收盘价可能还在更新，保持短 TTL 多抓几次
        return SETTLE_TTL
    return max(1, int((next_session_start(n) - n).total_seconds()))


def status(now=None):
    """给接口/页面用的一句话状态。"""
    n = _now(now)
    st = market_state(n)
    return {
        "state": st,
        "label": STATE_LABEL.get(st, st),
        "is_live": st == "open",          # 是否值得抓实时
        "ttl": quote_ttl(n),
        "trading_day": is_trading_day(n.date()),
        "now": n.strftime("%Y-%m-%d %H:%M:%S"),
        "next_session": next_session_start(n).strftime("%Y-%m-%d %H:%M"),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    s = status()
    print("现在:", s["now"])
    print("状态:", s["label"], "（是否值得抓实时: %s）" % s["is_live"])
    print("行情缓存 TTL: %d 秒（%.1f 小时）" % (s["ttl"], s["ttl"] / 3600))
    print("下一次有新行情:", s["next_session"])
    print()
    print("--- 抽样（同一天的各个时点）---")
    d = date(2026, 10, 5)   # 2026-10-05 是国庆假期内的周一
    for label, dt in (
        ("周一 08:00（假期）", datetime(2026, 10, 5, 8, 0)),
        ("周六 10:00", datetime(2026, 10, 3, 10, 0)),
        ("周一 09:20（工作日）", datetime(2026, 10, 12, 9, 20)),
        ("周一 10:00", datetime(2026, 10, 12, 10, 0)),
        ("周一 12:00（午休）", datetime(2026, 10, 12, 12, 0)),
        ("周一 14:00", datetime(2026, 10, 12, 14, 0)),
        ("周一 15:05", datetime(2026, 10, 12, 15, 5)),
        ("周一 16:00", datetime(2026, 10, 12, 16, 0)),
    ):
        print("  %-22s %-6s TTL=%9d 秒" % (label, market_state(dt), quote_ttl(dt)))
