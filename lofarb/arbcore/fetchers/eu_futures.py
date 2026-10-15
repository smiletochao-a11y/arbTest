# -*- coding: utf-8 -*-
"""
[AI-2026-10-08] 欧系指数期货取数（法国 CAC40-FCE / 德国 DAX-FDAX）—— T日实时估值用。
[AI-2026-10-09] 🔴 **investing.com 已彻底移除**（东哥：「我 investing 没有好感，不用他的数据」）。

为什么需要它
------------
法国(如 513080)、德国的基金跟踪欧系现货指数；现货 17:30(巴黎/柏林) 就收盘，
而对应期货交易到 22:00。所以 T 日估值增量 =「现货收盘时点(17:30)的期货价 → 当前期货价」的涨幅，
叠到 T-1 估值上。这与我们原油(USO/CL)的"现货收盘时点期货价 → 当前期货价"是同一思路。

🔴 为什么删掉 investing.com（2026-10-09 结论，勿再接回）
---------------------------------------------------
  10-08 曾判定「requests 被 403 是 TLS 指纹、curl 兜底能过」，**该结论已被推翻**。
  10-09 07:4x 重新实测（全部本机直连）：

  | 目标 | 结果 |
  |---|---|
  | `api.investing.com/api/financialdata/8826/historical/chart` | **403** |
  | `www.investing.com/` **首页** | **403**（连首页都进不去） |
  | 换 UA（iPhone / Googlebot / curl-8.4） | 仍 **403** |
  | 加全套浏览器头（sec-ch-ua / Sec-Fetch / Accept-Language…） | 仍 **403** |
  | 响应头 | `Server: cloudflare` + `CF-RAY: ...-LAX` + `__cf_bm` cookie |

  ⇒ **Cloudflare 按 IP 封禁**（非端点门禁、非 UA、非 TLS 指纹），本机 IP 已被拉黑，短期无解。
  本机 hook 代理（127.0.0.1:6116）又拦截不出境 ⇒ 无任何绕过路径。
  ⇒ 附带危害：`_get_json` 只缓存成功结果、**403 失败不缓存** ⇒ 每只基金每轮询（前�� 30s）
    都要重跑一遍注定失败的回捞，**单次阻塞 35 秒**。
  ⇒ 东哥拍板：**代码里彻底删除 investing 通道**，只记录上述封禁依据。

现存数据源（本机直连实测 200）
--------------------------------------------------
A) **onvista.de** 公开接口 —— 只提供"当前报价"（无历史）
     GET https://api.onvista.de/api/v1/instruments/FUTURE/{id}/quote
     返回 last / bid / ask / previousLast / datetimeLast / codeQualityPrice("DLY"=延迟)
     ✅ 10-09 实测 200。`datetimeLast` 为 **UTC（带 +00:00）**，延迟约 15 分钟。
     ⚠️ **只有 DAX(FDAX) 合约**，法国 CAC40-FCE 在 onvista **不存在**
       （实测 FUTURE/1187759 → HTTP 400；search 接口只返回 INDEX/COMMODITY，不返回期货）。

B) **boerse-frankfurt.de** 公开接口 —— 最新结算价（FDAX 专用，抗限流）
     GET https://api.boerse-frankfurt.de/v1/data/quote_box/single?isin=DE0008469594&mic=XEUR
     ✅ 10-09 实测 200 → {"lastPrice":24967, "timestampLastPrice":"...", ...}
     ⚠️ **只给「最新一个交易日」的结算价，无历史** ⇒ 仅当日期 == 所需 base_date 时可用。

法国 FCE 现状：无任何可用免费源（逐个实测排除）
--------------------------------------------------
  investing 1187759 → 403（IP 封）；onvista → 无此合约（400）；
  Euronext 官方 live.euronext.com → 产品页 302→404、API 全 404；
  stooq → JS proof-of-work 挑战（非纯 HTTP）。
  ⇒ **513080 恒显「—」（宁缺勿假，绝不兜底）**，等后续找到源再说。

设计原则
--------
  * 纯取数 + 解析，不落库、不管调度（与"放本地还是 ARM"解耦）
  * 锚点 = 当地现货收盘 17:30 那一刻的期货价；当前值 = 最新已出的期货报价
  * 汇率/基差一律不兜底；缺数据返回 None（宁缺勿假）
  * 🔴 **取不到就是取不到**：宁可显「—」，绝不用近似源/跨源拼凑冒充
"""

import calendar
import logging
import time
from datetime import datetime, timedelta, time as _dtime

try:
    import requests
    requests.packages.urllib3.disable_warnings()
except Exception:  # pragma: no cover
    requests = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 合约注册表
# ---------------------------------------------------------------------------
INSTRUMENTS = {
    "FDAX": {
        "name": "DAX 期货 (Eurex FDAX)",
        "onvista_id": "9289722",
        "bf_isin": "DE0008469594",
        "spot_close": "17:30",   # 现货(Xetra)收盘，当地时间
    },
    "FCE": {
        "name": "CAC40 期货 (Euronext FCE)",
        "onvista_id": None,       # onvista 无此合约
        "bf_isin": None,          # boerse-frankfurt 只覆盖 Xetra(DAX)
        "spot_close": "17:30",    # 现货(Euronext Paris)收盘，当地时间
    },
}

# 基金代码 → 期货品种（unified_fund_list 已核对）
FUND_TO_FUTURE = {
    "513080": "FCE",   # 法国ETF华安  (related_index=CAC)
    "513030": "FDAX",  # 德国ETF华安  (related_index=DAX)
    "159561": "FDAX",  # 德国ETF嘉实  (related_index=DAX)
}

_OV_BASE = "https://api.onvista.de/api/v1/instruments/FUTURE/{vid}/quote"
_OV_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 本机/沙箱环境常带 http_proxy=127.0.0.1:xxxx（死端口），必须显式绕开
_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None and requests is not None:
        s = requests.Session()
        s.trust_env = False                      # 忽略 HTTP_PROXY/HTTPS_PROXY 环境变量
        s.proxies = {"http": None, "https": None}
        s.verify = False
        _SESSION = s
    return _SESSION


# --- 节流 & 重试（现存两个源都很温和，1s 足够；investing 的 6s 节流已随该源一并移除）----
_MIN_INTERVAL = 1.0      # 同一 host 两次请求最小间隔（秒）
_RETRIES = 2
_LAST_CALL = {}          # host -> ts
_CACHE = {}
_CACHE_TTL = 55


def _throttle(host):
    last = _LAST_CALL.get(host, 0)
    wait = _MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[host] = time.time()


def _get_json(url, headers, host, params=None):
    """带节流 + 退避重试的 GET（纯 requests；无 curl 兜底——那是为 investing WAF 专设的）。"""
    sess = _session()
    if sess is None:
        return None
    full = url
    if params:
        from urllib.parse import urlencode
        full = f"{url}?{urlencode(params)}"
    for attempt in range(_RETRIES):
        _throttle(host)
        try:
            r = sess.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (attempt + 1))
                continue
            # 🔴 403 = 该源在封我们（实测 onvista/boerse-frankfurt 不封，仅历史移除的
            #    investing 会）。属降级告警 ⇒ WARNING + ⚠️，一次性打，不按轮询刷屏。
            lvl = logger.warning if r.status_code == 403 else logger.info
            lvl("⚠️ eu_futures %s HTTP %s: %s", host, r.status_code, r.text[:120])
            return None
        except Exception as e:
            logger.debug("eu_futures %s 请求异常: %s", host, e)
            time.sleep(0.6 * (attempt + 1))
    return None



# ---------------------------------------------------------------------------
# 时区：Europe/Paris == Europe/Berlin，EU DST 规则（3月最后周日 / 10月最后周日 01:00 UTC）
# ---------------------------------------------------------------------------
def _last_sunday(year, month):
    last = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last)
    while d.weekday() != 6:  # 6 = Sunday
        d -= timedelta(days=1)
    return d


def eu_utc_offset_hours(dt_utc):
    """欧陆(巴黎/柏林)在给定 naive-UTC 时刻的偏移小时（夏令 +2 / 冬令 +1）。"""
    y = dt_utc.year
    dst_start = _last_sunday(y, 3).replace(hour=1)
    dst_end = _last_sunday(y, 10).replace(hour=1)
    naive = dt_utc.replace(tzinfo=None)
    return 2 if dst_start <= naive < dst_end else 1


def anchor_utc(ref_date, spot_close="17:30"):
    """把「当地现货收盘时刻」换算成 naive UTC datetime。ref_date 为 date。"""
    hh, mm = [int(x) for x in spot_close.split(":")]
    dt_local = datetime.combine(ref_date, _dtime(hh, mm))
    off = eu_utc_offset_hours(dt_local - timedelta(hours=2))
    utc = dt_local - timedelta(hours=off)
    off2 = eu_utc_offset_hours(utc)
    if off2 != off:
        utc = dt_local - timedelta(hours=off2)
    return utc


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def _norm_count(n):
    if n < 1:
        return 1
    return n


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
# 🔴 [AI-2026-10-09 东哥拍板] investing 通道已整体删除 ⇒ 本模块**不再提供任何历史 K 线**。
#    原 fetch_chart / fetch_intraday / fetch_daily / _pick_bar / anchor_price / daily_close
#    全部依赖 investing 的 chart 接口，现已无源，**保留函数名但恒返回空**，
#    以免破坏 sampler / store / diagnose 的调用契约。
#    ⇒ 锚点唯一来源 = **采样器当日冻结**（eu_futures_quotes 表）＋ boerse-frankfurt 当日结算价。
#    ⇒ 法国 FCE 连"当日结算价"都没有（bf 只覆盖 DAX）⇒ 恒缺，显「—」，绝不兜底。

_REMOVED_WITH_INVESTING = (
    "fetch_chart", "fetch_intraday", "fetch_daily",
    "anchor_price", "daily_close",
)


def fetch_chart(symbol, interval="PT1M", count=120, use_cache=True):
    """🔴 已移除（investing 删除）：无历史 K 线源，恒返回 []。"""
    return []


def fetch_intraday(symbol, count=120):
    """🔴 已移除（investing 删除）：恒返回 []。"""
    return []


def fetch_daily(symbol, count=160):
    """🔴 已移除（investing 删除）：恒返回 []。"""
    return []


def current_quote(symbol, use_cache=True):
    """
    当前报价。**唯一来源 = onvista**（10-09 实测 200，~15min 延迟、codeQualityPrice="DLY"）。
    ⚠️ 法国 FCE 在 onvista 无合约 ⇒ 恒返回 None（宁缺勿假）。

    返回 dict：{'price','ts' (naive utc),'source','quality','previous'} 或 None。
    """
    meta = INSTRUMENTS.get(symbol)
    if not meta:
        return None
    vid = meta.get("onvista_id")
    if not vid:
        return None
    key = f"ov|{vid}"
    now = time.time()
    if use_cache and key in _CACHE and _CACHE[key][0] > now:
        return _CACHE[key][1]
    j = _get_json(_OV_BASE.format(vid=vid), _OV_HEADERS, "api.onvista.de")
    if j and j.get("last"):
        try:
            ts = datetime.strptime(j["datetimeLast"][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            ts = None
        res = {"price": j.get("last"), "ts": ts, "source": "onvista",
               "quality": j.get("codeQualityPrice"), "previous": j.get("previousLast")}
        if use_cache:
            _CACHE[key] = (now + _CACHE_TTL, res)
        return res
    return None


# ---------------------------------------------------------------------------
# 锚点 / T日涨幅
# ---------------------------------------------------------------------------
def _pick_bar(bars, target_utc, tolerance_min):
    best = None
    for b in bars:
        if b["ts"] <= target_utc:
            best = b
        else:
            break
    if best is None:
        return None
    if (target_utc - best["ts"]).total_seconds() / 60.0 > tolerance_min:
        return None
    return best


def anchor_price(symbol, ref_date, tolerance_min=45, count=160):
    """
    🔴 已移除（investing 删除）：17:30 精确 tick 只能靠采样器**当日冻结**入库，
    本函数无源可取，恒返回 (None, None)。锚点请走 `eu_futures_store.resolve_anchor`。
    """
    return None, None


def daily_close(symbol, ref_date, count=160):
    """
    🔴 已移除（investing 删除）：无历史日线源，恒返回 (None, None)。

    历史事实留档（供理解锚点口径，**不可再依赖**）：Eurex FDAX 日收盘≈当日结算价≈当地 17:30
    （10-08 实测 investing 8826 日线 10-07 收 25226 == boerse-frankfurt 结算价 25226）——
    这条正是"boerse-frankfurt 当日结算价可当锚点"的依据，但 bf 只给**最新**日。
    """
    return None, None


# ---------------------------------------------------------------------------
# 锚点备用源：boerse-frankfurt 最新结算价（FDAX 专用）
#   端点在 §004_7 文档中已登记：quote_box/single?isin=DE0008469594&mic=XEUR
#   → {"lastPrice":24967, "changeToPrevDayInPercent":-1.03, "timestampLastPrice":"..."}
#   ✅ 10-09 实测 200（investing 挂掉时它仍活 ⇒ 这就是当初多源设计的价值）。
#   ⚠️ 只给「最新一个交易日」的结算，**无历史** ⇒ 仅当日期与所需 base_date 相同才可用。
# ---------------------------------------------------------------------------
_BF_URL = "https://api.boerse-frankfurt.de/v1/data/quote_box/single?isin={isin}&mic=XEUR"
_BF_ISIN = {"FDAX": "DE0008469594"}


def bf_latest_settlement(symbol):
    """boerse-frankfurt 最新结算价。返回 (price, 'YYYY-MM-DD') 或 (None, None)。仅 FDAX。"""
    isin = _BF_ISIN.get(symbol)
    if not isin:
        return None, None
    j = _get_json(_BF_URL.format(isin=isin),
                  {"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                  "api.boerse-frankfurt.de")
    if not isinstance(j, dict):
        return None, None
    price = j.get("lastPrice")
    ts = str(j.get("timestampLastPrice") or "")[:10]
    return (float(price) if price else None), (ts or None)


def t_day_ratio(symbol, ref_date, tolerance_min=45):
    """
    T 日涨幅 = 当前期货价 / 锚点期货价 - 1。
    返回 dict(anchor, anchor_ts, current, current_ts, source, delay_min, ratio)。
    """
    a, ats = anchor_price(symbol, ref_date, tolerance_min=tolerance_min)
    q = current_quote(symbol)
    ratio = None
    if a and q and q.get("price") and a > 0:
        ratio = q["price"] / a - 1.0
    delay = None
    if q and q.get("ts"):
        delay = round((datetime.utcnow() - q["ts"]).total_seconds() / 60.0, 1)
    return {"anchor": a, "anchor_ts": ats,
            "current": (q or {}).get("price"), "current_ts": (q or {}).get("ts"),
            "source": (q or {}).get("source"), "delay_min": delay, "ratio": ratio}


def estimate_t(prev_value, symbol, ref_date, tolerance_min=45):
    """T 日估值 = T-1 估值 × (1 + T日期货涨幅)。缺数据返回 None。"""
    if prev_value is None:
        return None
    r = t_day_ratio(symbol, ref_date, tolerance_min=tolerance_min)
    return None if r["ratio"] is None else prev_value * (1.0 + r["ratio"])


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------
def diagnose(symbol=None):
    """逐品种体检：onvista 当前报价 +（FDAX）boerse-frankfurt 结算价 + 今日 17:30 锚点时刻。"""
    syms = [symbol] if symbol else list(INSTRUMENTS.keys())
    today = datetime.utcnow().date()
    for s in syms:
        meta = INSTRUMENTS[s]
        print(f"[{s}] {meta['name']}")
        print(f"    onvista_id={meta['onvista_id']}  bf_isin={meta.get('bf_isin')}")
        q = current_quote(s, use_cache=False)
        if q:
            delay = round((datetime.utcnow() - q["ts"]).total_seconds() / 60.0, 1) if q.get("ts") else None
            print(f"    ✅ onvista 报价 {q['price']}  quality={q['quality']}  "
                  f"ts={q['ts']} UTC  延迟≈{delay}min")
        else:
            print("    ❌ onvista 无报价（无该合约 or 取数失败）")
        bp, bd = bf_latest_settlement(s)
        if bp:
            print(f"    ✅ boerse-frankfurt 结算 {bp} @ {bd}"
                  + ("（**只给最新日**，仅当等于 base_date 才可当锚点）" if bd else ""))
        else:
            print("    ❌ boerse-frankfurt 无结算价（仅 FDAX 有）")
        print(f"    今日({today}) {meta['spot_close']} 当地收盘 → 锚点时刻 UTC {anchor_utc(today, meta['spot_close'])}")
        if meta["onvista_id"] is None and meta.get("bf_isin") is None:
            print("    ⚠️ 该品种**无任何可用源** ⇒ 实时估值恒显「—」（宁缺勿假，绝不兜底）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    diagnose()
