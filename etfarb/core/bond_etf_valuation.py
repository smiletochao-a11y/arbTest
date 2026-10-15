# -*- coding: utf-8 -*-
"""债券 ETF 盘中估值（移植自 arbTest BondETFValuation，2026-09-18 东哥授权）。

仅服务于「现金管理」TAB 的 4 只：511880 / 511990 / 511360 / 511520。
算法口径与 arbTest/LOF套利程序完全一致（东哥金标准），逐项照搬：

  · 511880 / 511990 银华日利 / 华宝添益（货币型）：`日均增长法`
      货币基金净值稳增，取近 20 日日均增量（每日计息，含周末/节假日），
      从最新净值往前累加到今日。

  · 511360 海富通短融（短久期）：`底仓票息 + 国债指数方向`
      est = latest_nav + daily_coupon(0.003) + 国债指数000012涨跌幅 × idx_coefficient(0.07)
      无国债指数源时退化为 latest_nav + daily_coupon。

  · 511520 富国政金（中久期）：`底仓票息 + 10Y国债期货方向`
      est = latest_nav + daily_coupon(0.02) + latest_nav × 活跃10Y国债期货涨跌幅%
      （合约按交割月动态取未到期最近月，2026-09-30 起不再硬编码 T2609；
       系数 1.0，与 arbTest estimate_today_nav 完全一致）

数据源（与 arbTest 对齐，但落地到 ETFarb 已验证通道）：
  · 净值历史：天天基金 f10/lsjz API（ETFarb 的 unified_fund_history(nav) 对这 3 只为空，
    必须现拉；arbTest 同款源）。进程内缓存 30 分钟。
  · 国债指数 000012：由调用方传入 `treasury_pct`（etf_iopv_quote 走
    index_quote.IndexQuoteProvider.get_change("000012")，即 s_sh000012，已实测通）。
  · 国债期货：本模块内新增新浪 CFF_RE_<活跃合约> 抓取（含 TF 辅助），
    活跃合约由 `_active_cffex_code` 动态计算（取代写死的 T2609）。
"""
import calendar
import json
import logging
import re
import ssl
import time
import threading
import urllib.request
from datetime import date, datetime, timedelta

logger = logging.getLogger("bond_etf_valuation")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BOND_ETF_META = {
    '511880': {'name': '银华日利ETF', 'type': 'money_market', 'weekend_on': 'friday'},
    '511990': {'name': '华宝添益ETF', 'type': 'money_market', 'weekend_on': 'friday'},
    '511360': {'name': '短融ETF海富通', 'type': 'short_bond', 'weekend_on': 'monday',
               'daily_coupon': 0.003, 'idx_coefficient': 0.07},
    '511520': {'name': '政金债ETF富国', 'type': 'mid_bond', 'weekend_on': 'monday',
               'daily_coupon': 0.02},
}

NAV_CACHE_TTL = 1800      # 净值历史缓存 30 分钟
_IDX_CACHE_TTL = 30       # 期货行情缓存 30 秒


# ── 中金所国债期货：活跃合约动态计算（取代硬编码 T2609，2026-09-30）──
def _cffex_delivery_months():
    return (3, 6, 9, 12)


def _second_friday(y, m):
    """CFFEX 国债期货最后交易日 = 交割月第 2 个周五（用于判断是否已到期）。"""
    first = date(y, m, 1)
    off = (4 - first.weekday()) % 7
    return date(y, m, 1 + off + 7)


def _active_cffex_code(prefix="T", as_of=None):
    """取未到期的最近交割月合约代码（如 2026-09 → T2612）。

    as_of 用于历史弹窗：按估值日取当时活跃合约，实现自动换月。"""
    as_of = as_of or date.today()
    cands = []
    for y in (as_of.year, as_of.year + 1):
        for m in _cffex_delivery_months():
            if y == as_of.year and m < as_of.month:
                continue
            if _second_friday(y, m) >= as_of:
                cands.append("%s%02d%02d" % (prefix, y % 100, m))
    return cands[0] if cands else "%s%02d12" % (prefix, as_of.year % 100)


# ── 交易日历（国庆调休等；⚠️ 2026 具体安排需东哥核对官方通知）──
_CN_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25",                     # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",   # 国庆 10-1~7（用户明示）
}
_CN_MAKEUP_2026 = {                # 调休上班的周末（视为交易日）
    "2026-09-27", "2026-10-10", "2026-10-11",
}


def _is_trading_day(d):
    s = d.strftime("%Y-%m-%d")
    if s in _CN_MAKEUP_2026:
        return True
    if d.weekday() >= 5:
        return False
    return s not in _CN_HOLIDAYS_2026



# ── 净值历史：天天基金 f10/lsjz ──
def _fetch_nav_history(code, days=30):
    url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
           "&pageIndex=1&pageSize=%d" % (code, days))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code,
    })
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
    s = raw.find("{"); e = raw.rfind("}") + 1
    if s < 0 or e <= s:
        return []
    data = json.loads(raw[s:e])
    recs = data.get("Data", {}).get("LSJZList", [])
    out = []
    for r in recs:
        d = r.get("FSRQ"); nav = r.get("DWJZ")
        if d and nav:
            try:
                out.append((d, float(nav)))
            except (ValueError, TypeError):
                continue
    return out


def _latest_nav_qq_jj(code):
    """腾讯 jj 实时单位净值（字段[3]）。用于 eastmoney lsjz 尺度异常的基金
    （如 511990：东财返 0.275 而非 ~100；腾讯 year_all 为空）。返回 float 或 None。"""
    try:
        raw = urllib.request.urlopen(urllib.request.Request(
            "https://qt.gtimg.cn/q=sh%s" % code,
            headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"}),
            timeout=8, context=_CTX).read().decode("gbk", "ignore")
        m = re.search(r'v_sh%s="([^"]+)"' % code, raw)
        if not m:
            return None
        return float(m.group(1).split("~")[3])
    except Exception:
        return None


class _BondNavCache:
    """进程内净值历史缓存（按 code_days）。"""
    def __init__(self):
        self._nav = {}
        self._lock = threading.Lock()

    def get_history(self, code, days=30):
        key = "%s_%d" % (code, days)
        with self._lock:
            cached = self._nav.get(key)
            if cached and time.monotonic() - cached[0] < NAV_CACHE_TTL:
                return cached[1]
        recs = _fetch_nav_history(code, days)
        if recs:
            with self._lock:
                self._nav[key] = (time.monotonic(), recs)
        return recs


_nav_cache = _BondNavCache()


def calc_avg_daily_growth(history, weekend_on, days=20):
    """history: [(date_str, nav), ...] 降序（[0] 最新）。返回日均增量或 None。"""
    if len(history) < 3:
        return None
    daily_rates = []
    for i in range(len(history) - 1, 0, -1):
        older = history[i]
        newer = history[i - 1]
        nav_change = newer[1] - older[1]
        try:
            d1 = datetime.strptime(older[0], "%Y-%m-%d")
            d2 = datetime.strptime(newer[0], "%Y-%m-%d")
            delta_days = (d2 - d1).days
        except ValueError:
            delta_days = 1
        # 货币型每日计息（含周末/节假日），accrual = 自然日天数
        accrual = max(delta_days, 1)
        daily_rates.append(nav_change / accrual)
    if not daily_rates:
        return None
    recent = daily_rates[-min(len(daily_rates), days):]
    if len(recent) >= 4:
        mean = sum(recent) / len(recent)
        std = (sum((x - mean) ** 2 for x in recent) / len(recent)) ** 0.5
        filtered = [x for x in recent if abs(x - mean) < 5 * std]
        if len(filtered) >= 3:
            recent = filtered
    return sum(recent) / len(recent)


# ── 国债期货 T2609 / TF2609（新浪）──
_futures_cache = {"t": 0.0, "v": None}
_futures_lock = threading.Lock()


def get_treasury_futures_quote():
    """返回 (t_pct, tf_pct, t_price) 或 (None, None, None)。进程内缓存 30s。

    t_price = 活跃 10Y 国债期货最新价（「指数价」列展示用）。
    合约按交割月动态取未到期最近月（2026-09-30 起不再硬编码 T2609）。"""
    now = time.monotonic()
    with _futures_lock:
        if _futures_cache["v"] is not None and now - _futures_cache["t"] < _IDX_CACHE_TTL:
            v = _futures_cache["v"]
            return v[0], v[1], v[2] if len(v) > 2 else None
    t_pct = tf_pct = t_price = None
    try:
        t_code = _active_cffex_code("T")
        tf_code = _active_cffex_code("TF")
        url = "https://hq.sinajs.cn/list=CFF_RE_%s,CFF_RE_%s" % (t_code, tf_code)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=8, context=_CTX).read().decode("gbk", "ignore")
        for line in raw.strip().splitlines():
            if '="' not in line:
                continue
            fields = line.split('"')[1].split(",")
            if len(fields) < 10:
                continue
            try:
                latest = float(fields[0])
                yest_settle = float(fields[1])
            except ValueError:
                continue
            if yest_settle <= 0:
                continue
            pct = (latest - yest_settle) / yest_settle * 100
            if 'CFF_RE_%s' % t_code in line:
                t_pct = round(pct, 4)
                t_price = latest
            elif 'CFF_RE_%s' % tf_code in line:
                tf_pct = round(pct, 4)
    except Exception:
        pass
    res = (t_pct, tf_pct, t_price)
    with _futures_lock:
        _futures_cache["t"] = now
        _futures_cache["v"] = res
    return res


def get_treasury_futures_pct():
    """兼容旧调用：返回 (t_pct, tf_pct) 或 (None, None)。"""
    t, tf, _ = get_treasury_futures_quote()
    return t, tf


# ── 对外：单只估值 ──
def estimate_bond_nav(code, treasury_pct=None, futures_pct=None):
    """返回 dict：{est, idx_chg, index_nm, method, note, latest_nav, nav_date}。

    est      日内预估净值（None=取数失败，调用方退化为最新净值）
    idx_chg  展示用「指数涨幅」：511360=国债指数%、511520=活跃10Y国债期货%、511880=None
    index_nm 对应指数名（前端展示）；511880=None
    """
    meta = BOND_ETF_META.get(code)
    out = {"est": None, "idx_chg": None, "index_nm": None,
           "method": "unknown", "note": None, "latest_nav": None, "nav_date": None}
    if not meta:
        out["note"] = "未知债券基金 %s" % code
        return out

    history = _nav_cache.get_history(code, days=30)
    if not history:
        out["note"] = "净值历史获取失败（天天基金）"
        return out
    latest_date, latest_nav = history[0]
    out["latest_nav"] = latest_nav
    out["nav_date"] = latest_date
    today = datetime.now().strftime("%Y-%m-%d")
    # 511990 等 eastmoney lsjz 返错误尺度（0.275 而非 ~100）；腾讯 year_all 为空。
    # 改用腾讯 jj 实时净值（仅当前值，无历史序列）。
    if latest_nav is None or latest_nav < 50:
        jj_nav = _latest_nav_qq_jj(code)
        if jj_nav:
            out["latest_nav"] = jj_nav
            out["nav_date"] = today
            out["est"] = round(jj_nav, 4)
            out["method"] = "jj_realtime_no_history"
            out["note"] = ("%s eastmoney净值尺度异常，改用腾讯jj实时净值"
                           "（无历史序列，仅展示当前值）" % code)
            out["idx_chg"] = None
            out["index_nm"] = None
            return out

    # 🔴 源心跳：净值日期滞后 >1 个交易日（跳过周末/节假日），提示源更新中断。
    if out["nav_date"]:
        try:
            _nd = datetime.strptime(out["nav_date"], "%Y-%m-%d").date()
            _gap = 0
            _cur = _nd
            while _cur < date.today():
                _cur += timedelta(days=1)
                if _is_trading_day(_cur):
                    _gap += 1
            if _gap > 1:
                logger.warning("[bond] %s 净值日期滞后 %d 个交易日（nav_date=%s），疑似源更新中断",
                               code, _gap, out["nav_date"])
        except ValueError:
            pass

    # ══ 511360：票息 + 国债指数线性 ══
    if code == '511360':
        daily_coupon = meta.get('daily_coupon', 0.003)
        idx_coeff = meta.get('idx_coefficient', 0.07)
        _cp = daily_coupon if _is_trading_day(date.today()) else 0.0
        if treasury_pct is None:
            est = latest_nav + _cp
            out["method"] = "coupon_only_no_index"
        else:
            idx_adj = round(treasury_pct * idx_coeff, 4)
            est = latest_nav + _cp + idx_adj
            out["method"] = "coupon+treasury_linear"
        out["est"] = round(est, 4)
        out["idx_chg"] = treasury_pct
        out["index_nm"] = "国债指数"
        return out

    # ══ 511520：票息 + 活跃 10Y 国债期货方向 ══
    # 🔴 方向信号优先级：活跃 10Y 国债期货（arbTest 口径，新浪，合约按交割月动态取）>
    #   国债指数000012（同向代理，sina 限频时退化，二者同跟踪利率、方向一致）。
    #   确保 sina 跪时仍有方向，不全退化成净值。
    if code == '511520':
        daily_coupon = meta.get('daily_coupon', 0.02)
        _cp = daily_coupon if _is_trading_day(date.today()) else 0.0
        if futures_pct is not None:
            sig = futures_pct
            sig_nm = "10Y国债期货%s" % _active_cffex_code("T")
            out["method"] = "coupon+futures_active"
        elif treasury_pct is not None:
            sig = treasury_pct
            sig_nm = "国债指数(代理)"
            out["method"] = "coupon+treasury_proxy"
        else:
            sig = 0.0
            sig_nm = "国债指数(代理)"
            out["method"] = "coupon_only_no_index"
            # 🔴 源心跳：活跃10Y国债期货(新浪) 与 国债指数000012(代理) 双源皆失效，
            #   511520 退化为纯票息（无方向信号）。记 log 便于发现抓取链路中断。
            logger.warning("[bond] 511520 方向信号双源失效：活跃%s期货与国债指数000012"
                           "均不可得，退化为纯票息估值" % _active_cffex_code("T"))
        futures_adj = latest_nav * sig / 100.0
        est = latest_nav + _cp + futures_adj
        out["est"] = round(est, 4)
        out["idx_chg"] = sig
        out["index_nm"] = sig_nm
        return out

    # ══ 511880 / 511990 / 其他货币型：日均增长法（每个自然日都计息）══
    avg = calc_avg_daily_growth(history, meta.get('weekend_on'), days=20)
    out["method"] = "avg_daily_growth"
    if avg is None:
        out["est"] = latest_nav
        out["method"] = "latest_only"
        return out
    try:
        last_dt = datetime.strptime(latest_date, "%Y-%m-%d")
    except ValueError:
        out["est"] = latest_nav
        return out
    est = latest_nav
    cur = last_dt + timedelta(days=1)
    while cur.strftime("%Y-%m-%d") <= today:
        est += avg          # 货币型每个自然日都计息（含周末/节假日）
        cur += timedelta(days=1)
    out["est"] = round(est, 4)
    out["idx_chg"] = None
    out["index_nm"] = None
    return out


# ════════════════════════════════════════════════════════════════════
# 静态估值历史（「静态估值」列 + 点击弹核对页，2026-09-19 东哥要求对齐 LOF 程序）
#
# 口径与 LOF 的 static_val 引擎同构（公布 nav(D) 之前的预估，随后核对答案）：
#   static_val(D) = nav(D-1) + 票息 + 方向项(D)
#     · 511880：nav(D-1) + 日均增量 × 计息天数(D-1→D，周末效应同日均增长法)
#     · 511360：nav(D-1) + 0.003 + 0.07 × 国债指数000012日涨幅%
#     · 511520：nav(D-1) + 0.02 + nav(D-1) × 方向%(D)/100
#       方向：活跃 10Y 国债期货日K（新浪 InnerFuturesNewService，按估值日动态取合约）优先；
#       缺失日回退国债指数000012 同向代理 —— 与实时估值的退化优先级完全一致。
#   err_pct = (nav(D) ÷ static_val(D) − 1) × 100
# 净值源 = ETFarb 自有 `fund_nav_history`（腾讯 year_all，174 行/只）——
# 单一事实源，不再走天天基金（避免双源数字打架）。
# ════════════════════════════════════════════════════════════════════
_FUT_KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                  "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}")
_SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20d=/"
                   "CN_MarketDataService.getKLineData?symbol={sym}&scale=240&ma=no&datalen=200")
# 🔴 上面两个 URL 模板含 `%20`，绝不能用 `url % code` 做替换——`%20t`/`%20d`
#   会被 Python 当格式符抛 TypeError（2026-09-19 踩坑：K 线静默返回空）。统一 .format()。
_fut_kline_cache = {"t": 0.0, "v": None}
_fut_kline_lock = threading.Lock()
_tr_kline_cache = {"t": 0.0, "v": None}


def _fetch_futures_kline(symbol="T2609", ttl=1800):
    """T2609 日K收盘 {date: close}。进程内缓存 30min；失败返回 {}。"""
    now = time.monotonic()
    with _fut_kline_lock:
        if _fut_kline_cache["v"] is not None and symbol in _fut_kline_cache["v"] \
                and now - _fut_kline_cache["t"] < ttl:
            return _fut_kline_cache["v"][symbol]
    out = {}
    try:
        req = urllib.request.Request(_FUT_KLINE_URL.format(sym=symbol), headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("(["); e = raw.rfind("])")
        if s >= 0 and e > s:
            for it in json.loads(raw[s + 1:e + 1]):
                try:
                    out[str(it["d"])] = float(it["c"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        pass
    with _fut_kline_lock:
        # 🔴 只在成功取到数据时写缓存，避免新浪限频返回的空 {} 污染 30min 缓存
        #    （否则一次限频会让后续 30 分钟内 511520 弹窗全退化成国债指数代理）。
        if out:
            if _fut_kline_cache["v"] is None:
                _fut_kline_cache["v"] = {}
            _fut_kline_cache["v"][symbol] = out
            _fut_kline_cache["t"] = now
    return out


def _fetch_treasury_kline_sina():
    """国债指数000012 日K（新浪 CN_MarketDataService，200根）。进程内缓存 30min。

    腾讯 fqkline 2026-09-19 晚实测对全部 A 股指数 501（临时限频？）⇒ 债券方向
    历史不能等它；新浪兜底并回写 index_history（source='sina_kline'），
    消费侧仍只认 index_history 一张表。"""
    return _fetch_sina_kline("sh000012")


_kline_cache = {}                       # {sym: {"t": monotonic, "v": {date: close}}}
_kline_lock = threading.Lock()


def _fetch_sina_kline(sym, ttl=1800):
    """通用新浪日K收盘 {date: close}（基金/指数都走这个口），缓存 30min。"""
    now = time.monotonic()
    with _kline_lock:
        hit = _kline_cache.get(sym)
        if hit and now - hit["t"] < ttl:
            return hit["v"]
    out = {}
    try:
        req = urllib.request.Request(_SINA_KLINE_URL.format(sym=sym), headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("(["); e = raw.rfind("])")
        if s >= 0 and e > s:
            for it in json.loads(raw[s + 1:e + 1]):
                try:
                    out[str(it["day"])[:10]] = float(it["close"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        pass
    with _kline_lock:
        # 🔴 只在取到数据时写缓存，避免限频空结果污染 30min 缓存
        if out:
            _kline_cache[sym] = {"t": now, "v": out}
    return out


def fetch_fund_kline(code, ttl=1800):
    """场内基金日收盘 {date: close}（新浪 CN_MarketDataService）。

    沪/深前缀未知 ⇒ 先试 sh 再试 sz，取非空者（LOF 沪深都有）。"""
    for pfx in ("sh", "sz"):
        rows = _fetch_sina_kline(pfx + code, ttl)
        if rows:
            return rows
    return {}


def clear_caches():
    """「核对静态估值（补采+重算）」入口：清空全部进程内数据缓存，强制回源。"""
    with _kline_lock:
        _kline_cache.clear()
        _fut_kline_cache["t"] = 0.0
        _fut_kline_cache["v"] = None
    _tr_kline_cache["t"] = 0.0
    _tr_kline_cache["v"] = None
    with _nav_cache._lock:
        _nav_cache._nav.clear()


def _treasury_series(db=None):
    """国债指数000012 日收盘 {date: close}（ETFarb `index_history` 单一事实源）。

    1) 先按 static_val 通道过期即补（腾讯 fqkline，12h TTL）；
    2) 库内仍 <2 行 ⇒ 新浪日K兜底并 INSERT OR REPLACE 回写 index_history；
    3) 再读库。两步都空 ⇒ {}（上层宁空勿错）。"""
    import sqlite3
    try:
        import static_val as _sv
        _sv.ensure_index_history(["000012"], db)
    except Exception:
        pass
    own = isinstance(db, str)
    c = sqlite3.connect(db) if own else db

    def _read():
        return {d: v for d, v in c.execute(
            "SELECT date, close FROM index_history WHERE symbol='000012' ORDER BY date")}

    try:
        rows = _read()
        if len(rows) < 2:
            sina = _fetch_treasury_kline_sina()
            if len(sina) >= 2:
                c.executemany(
                    """INSERT OR REPLACE INTO index_history (symbol, date, close, source)
                       VALUES ('000012', ?, ?, 'sina_kline')""",
                    [(d, v) for d, v in sina.items()])
                c.commit()
                rows = _read()
        return rows
    finally:
        if own:
            c.close()


def _accrual_days(d1, d2, weekend_on=None):
    """D-1→D 的计息天数（自然日，货币型每日计息）。weekend_on 保留兼容，不再使用。"""
    return max((d2 - d1).days, 1)


def _close_on_or_before(series, date):
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return (best, series[best]) if best else (None, None)


def static_history_bond(code, days=30, db=None, refresh=False):
    """债券型静态估值历史（date 降序），行形状对齐 arbTest 历史核对页：

      {date, nav, nav_chg, price, price_chg, yield_per_wan, premium_pct,
       static_val, val_error, val_error_rate, err_pct,
       idx_close, idx_pct (511360) / futures_close, futures_pct (511520)}

    口径（与 arbTest 历史弹窗同列同名）：
      yield_per_wan = (nav − price) × 100「折价几根毛」
      premium_pct   = (price ÷ nav − 1) × 100「溢价」
      val_error     = static_val − nav「估值误差」
      val_error_rate= (static_val − nav) ÷ nav × 100「误差率」
    数据不足（净值 <2 行 / 511360 无国债指数历史）的日期不出行 —— 宁空勿错。"""
    import sqlite3
    if refresh:
        clear_caches()
    meta = BOND_ETF_META.get(code)
    if not meta:
        return []
    c = sqlite3.connect(db) if isinstance(db, str) else db
    try:
        navs = [(d, v) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
    finally:
        if c is not db:
            c.close()
    if len(navs) < 2:
        return []
    dates = [d for d, _ in navs]
    weekend_on = meta.get('weekend_on')

    # 收盘价序列（一次性）：{date: (close, chg%)}，chg 相对上一K线日
    px_raw = fetch_fund_kline(code)
    px_dates = sorted(px_raw)
    px = {}
    for j, pd in enumerate(px_dates):
        prev_px = px_raw[px_dates[j - 1]] if j > 0 and px_raw[px_dates[j - 1]] else None
        px[pd] = (px_raw[pd],
                  (px_raw[pd] / prev_px - 1) * 100 if prev_px else None)

    # 方向序列（一次性取好）
    tr = {}
    if code == '511360':
        tr = _treasury_series(db)
        if len(tr) < 2:
            return []
    elif code == '511520':
        # 活跃 10Y 国债期货：循环内按「估值日」取当时活跃合约（自动换月），
        # 复用按合约的进程内缓存（已修复空值不污染）。
        # 🔴 优先抓取「当前活跃合约」并缓存：近期日期全用它，新浪限频时先保住它；
        #   老合约（换月前的 T2603/06/09）在循环内懒抓取，失败仅该历史行降级国债指数代理。
        _cur_cc = _active_cffex_code("T")
        _fetch_futures_kline(_cur_cc)
        tr = _treasury_series(db)          # 代理回退源

    # 511880：日均增量只依赖全历史，循环外算一次
    avg_511880 = None
    if code == '511880':
        avg_511880 = calc_avg_daily_growth(list(reversed(navs)), weekend_on, days=20)

    out = []
    for i in range(1, len(dates)):
        d, d0 = dates[i], dates[i - 1]
        nav_prev = navs[i - 1][1]
        nav_cur = navs[i][1]
        static = None
        idx_close = idx_pct = fut_close = fut_pct = None
        if code == '511880':
            if avg_511880 is None:
                continue
            try:
                accr = _accrual_days(datetime.strptime(d0, "%Y-%m-%d"),
                                     datetime.strptime(d, "%Y-%m-%d"), weekend_on)
            except ValueError:
                continue
            static = nav_prev + avg_511880 * accr
        elif code == '511360':
            b_d, b_px = _close_on_or_before(tr, d0)
            c_d, c_px = _close_on_or_before(tr, d)
            if not b_px or not c_px:
                continue
            idx_close, idx_pct = c_px, (c_px / b_px - 1) * 100
            static = nav_prev + meta.get('daily_coupon', 0.003) + \
                meta.get('idx_coefficient', 0.07) * idx_pct
        else:  # 511520
            pct = None
            cc = _active_cffex_code("T", as_of=datetime.strptime(d, "%Y-%m-%d").date())
            fut = _fetch_futures_kline(cc)          # 按合约缓存，逐日复用
            if d in fut:
                prev_fd = [x for x in sorted(fut) if x < d]
                if prev_fd and fut[prev_fd[-1]] > 0:
                    pct = (fut[d] / fut[prev_fd[-1]] - 1) * 100
                    fut_close, fut_pct = fut[d], pct
            if pct is None and len(tr) >= 2:
                b_d, b_px = _close_on_or_before(tr, d0)
                c_d, c_px = _close_on_or_before(tr, d)
                if b_px and c_px:
                    pct = (c_px / b_px - 1) * 100
            if pct is None:
                continue
            static = nav_prev + meta.get('daily_coupon', 0.02) + nav_prev * pct / 100.0
        if static is None or static <= 0:
            continue
        p = px.get(d, (None, None))
        price, price_chg = (p if p else (None, None))
        row = {
            "date": d, "nav": nav_cur,
            "nav_chg": (nav_cur / nav_prev - 1) * 100 if nav_prev else None,
            "price": price, "price_chg": price_chg,
            "yield_per_wan": (nav_cur - price) * 100 if (nav_cur and price) else None,
            "premium_pct": (price / nav_cur - 1) * 100 if (nav_cur and price) else None,
            "static_val": round(static, 4),
            "val_error": static - nav_cur,
            "val_error_rate": (static - nav_cur) / nav_cur * 100 if nav_cur else None,
            "err_pct": (nav_cur / static - 1) * 100,
        }
        if code == '511360':
            row["idx_close"], row["idx_pct"] = idx_close, idx_pct
        if code == '511520':
            row["futures_close"], row["futures_pct"] = fut_close, fut_pct
        out.append(row)
    return list(reversed(out))[:days]


def latest_static_bond(code, db=None):
    """看板行用：最新一日债券静态估值。返回 dict 或 None。"""
    rows = static_history_bond(code, days=2, db=db)
    return rows[0] if rows else None


if __name__ == "__main__":
    import sys
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    tz = None
    tf = None
    try:
        from index_quote import IndexQuoteProvider
        tz = IndexQuoteProvider().get_change("000012")
        tz = tz["pct"] if tz else None
    except Exception as e:
        print("treasury idx err", e)
    tf, _ = get_treasury_futures_pct()
    for c in ["511880", "511360", "511520"]:
        print(c, estimate_bond_nav(c, tz, tf))
    print("\n==== static history (last 5) ====")
    import os
    import db as _dbm   # noqa: E402  单库方案 arb_master.db
    _db = _dbm.DB_PATH
    for c in ["511880", "511360", "511520"]:
        rows = static_history_bond(c, days=5, db=_db)
        print(c, "rows=%d" % len(rows))
        for r in rows[-3:]:
            print("   %s nav=%.4f static=%.4f err=%+.3f%%" %
                  (r["date"], r["nav"], r["static_val"], r["err_pct"]))
