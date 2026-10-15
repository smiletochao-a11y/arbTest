# -*- coding: utf-8 -*-
"""ETFarb 场内份额 —— 交易所直采（2026-09-28 立项，取代东京 VPS 份额同步）。

四类标的 / 三个源（2026-09-28 本机实测钉死）
--------------------------------------------
| 类别                 | 源                                                | 单位     | 可补历史 |
|----------------------|---------------------------------------------------|----------|----------|
| sz_lof  16xxxx       | 深交所 scsj_fund_jjgm&jjlb=LOF（txtStart/End 窗） | 万份     | ✅       |
| sz_etf  158/159xxx   | 深交所 scsj_fund_jjgm&jjlb=ETF（txtStart/End 窗） | 万份     | ✅       |
| sh_lof  501/502/506  | 上交所 LOFGMTJ&SEARCH_DATE（逐日，一天全量 119）  | 万份     | ✅ 逐日  |
| sh_etf  51/52/56/58  | 腾讯 qt.gtimg.cn field[72]（实时快照）            | 份→万份  | ❌ 仅当天|

实测精度（与交易所官方逐位核对）
--------------------------------
· 深交所 `current_size` 直接就是**万份**（161226 = 365,446.05），与官方 xlsx 逐位吻合。
· 上交所 `INTERNAL_VOL` 同样是**万份**（501089 = 5,084.44）。
· 腾讯 `field[72]` = **总份额（份）**：159687 = 1,683,428,658 ÷ 1e4 = 168,342.87 万份，
  与深交所官方 168,342.86 仅差 0.002%；159366 -0.07%、513500 +0.29%、513000 +0.47%。
  （交易所对沪市 ETF 不公布份额 ⇒ 只能用此派生值，前端须标注口径。）

架构红线
--------
· ETFarb **不再读东京 VPS 份额**（VPS 份额脚本仍供 arbTest，**不动**）。
· 本机 / ARM **各自部署、各自采**，但落库统一进共享单库 `arb_master.db` 的
  `unified_fund_history`（单库方案，2026-10-05 拍板；与 arbTest 同文件、各写各列）。
· 落库唯一入口 `unified_fund_history`；`pre_share`→`shares`(万份)、`new_share`→`shares_added`
  自动求差（无更早基准留空）；in_arbtest=1 的 48 只份额归 arbTest，本模块跳过。
· 绝不兜底：取不到即跳过/暴露，不填 0、不倒算、不猜。
· 取数函数（`classify` / `fetch_szse_history` / `fetch_sse_lof_day` / `fetch_tencent_etf` / `collect`）
  **无 DB 依赖**，供 arbTest 同源复用；落库类 `ShareDirectFetcher` 才是 ETFarb 专属。
· arbTest `daily_updater.step9` 现在**直接 import 本模块的 `collect()`**（单一真源），
  不再维护 `arbcore/fetchers/share_direct_source.py` 副本。

运行
----
  python core/share_direct_fetcher.py --sync                 # 全库：深沪 LOF/ETF，补最近 30 天
  python core/share_direct_fetcher.py --sync --days 7        # 只补最近 7 天
  python core/share_direct_fetcher.py --sync --market L      # 只采 L 池（国内 LOF）
  python core/share_direct_fetcher.py --sync --codes 161226,513100
  python core/share_direct_fetcher.py --sync --dry           # 只抓不落库
  python core/share_direct_fetcher.py --report               # 打印份额覆盖报告
"""
import datetime
import io
import json
import os
import random
import re
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PROJ = os.path.dirname(_HERE)
DATA_DIR = os.path.join(PROJ, "data")

import db as _db   # noqa: E402  core/db.py —— 导入即 ensure_schema()
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）


def _is_in_arbtest(code):
    """分区写红线：in_arbtest=1 的 48 只份额归 arbTest，ETFarb 的 save_rows 跳过。"""
    try:
        c = _db.get_conn()
        try:
            r = c.execute("SELECT in_arbtest FROM fund_profile WHERE fund_code=?",
                          (str(code),)).fetchone()
            return bool(r and r[0])
        finally:
            c.close()
    except Exception:
        return False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 🔴 显式禁用代理：本机环境变量 http_proxy=127.0.0.1:12761 是死端口，会把请求打成 502
_PROXIES = {"http": None, "https": None}

# source 标记（落库口径可追溯）
SRC_SZSE = "szse_jjgm"          # 深交所官方（深 LOF + 深 ETF 同口径同源）
SRC_SSE = "sse_lofgm"           # 上交所官方 LOFGMTJ
SRC_TX = "tencent_derived"      # 腾讯派生（沪市 ETF，非官方）

SZSE_URL = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            "&CATALOGID=scsj_fund_jjgm&jjlb={jjlb}"
            "&txtDm={code}&txtStart={start}&txtEnd={end}&PAGENO={page}"
            "&random={rnd}")
# 🔴 headers 照抄东京 VPS `SH_SZ_LOFshares.py::fetch_szse_shares`（跑了数月未被封）。
# 2026-09-28 教训：旧版只带 UA/Referer/X-Request-Type，缺 `X-Requested-With`
# 等浏览器 AJAX 标志，被深交所 WAF 判定为脚本 ⇒ 高频后整段封 IP。
SZSE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.szse.cn/market/fund/list/index.html",
    "X-Request-Type": "ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}
SSE_URL = ("https://query.sse.com.cn/commonQuery.do?isPagination=true"
           "&sqlId=COMMON_SSE_SJ_JJSJ_JJGM_LOFGMTJ_L&PRODUCT_TYPE=11,14,15"
           "&SEARCH_DATE={date8}&type=inParams&pageHelp.pageSize=10000")
SSE_HEADERS = {"User-Agent": _UA, "Referer": "https://www.sse.com.cn/"}
TX_URL = "https://qt.gtimg.cn/q={sym}"
TX_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}

MAX_WINDOW_DAYS = 150           # 深交所日期窗上限（约 6 个月，超窗须分段）


class ShareSourceError(Exception):
    """份额取不到。绝不兜底。"""
    pass


# 🔴 深交所反爬极严。**必须照抄 VPS `SH_SZ_LOFshares.py` 的姿势**（Session 保连接 +
#    完整浏览器 headers（含 X-Requested-With）+ `random=` 破缓存 + 2~4s 间隔 + 一天一次）。
# 2026-09-28 教训：本机旧版只带 UA/Referer/X-Request-Type、固定 1.8s、且反复多轮
#    测试，累计 ~800 请求后被深交所整段拒绝（连 `www.szse.cn/` 根都 000），数小时未解封；
#    而同期 VPS 每天 390 只只跑一次、日志实测 5~8s/只，从未被封。
# ⇒ 默认间隔深市 3.0s / 沪LOF 0.6s / 腾讯 0.15s，并由 `_sleep()` 抖动；
#    连续失败达阈值视为限流，中断该类止损（下次再补，不硬刚）。
_CIRCUIT_FAILS = 12


def _sleep(base):
    """抖动休眠（模拟人类节奏，缓解交易所反爬）。"""
    if base and base > 0:
        time.sleep(base * (0.6 + random.random() * 0.8))


# ============================ 取数（无 DB 依赖） ============================
def classify(code):
    """标的分类 → 'sz_lof' | 'sz_etf' | 'sh_lof' | 'sh_etf' | None。

    规则（与沪深代码段一致，注意先判 LOF 再判 ETF，因 502 属 LOF 而 520/526 属 ETF）：
      · 501/502/506  → sh_lof    （沪市 LOF）
      · 51/52/56/58  → sh_etf    （沪市 ETF：510/511/512/513/515/516/518/520/526/560/561/562/563/588/589…）
      · 158/159      → sz_etf    （深市 ETF）
      · 16xxxx       → sz_lof    （深市 LOF）
    """
    c = str(code).strip()
    if len(c) != 6 or not c.isdigit():
        return None
    if c[:3] in ("501", "502", "506"):
        return "sh_lof"
    if c[:2] in ("51", "52", "56", "58"):
        return "sh_etf"
    if c[:3] in ("158", "159"):
        return "sz_etf"
    if c[:2] == "16":
        return "sz_lof"
    return None


def _num(s):
    """'365,446.05' → 365446.05；空/'-'/异常 → None。"""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-", "—", "--", "null", "None"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _iso(d):
    """'20260924' / '2026-09-24' → '2026-09-24'。"""
    s = str(d or "").strip()
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


_SESSION = None


def _session():
    """复用 `requests.Session`（照抄 VPS `SH_SZ_LOFshares.py`：连接保持、更像浏览器）。"""
    global _SESSION
    if _SESSION is None:
        import requests
        requests.packages.urllib3.disable_warnings()
        _SESSION = requests.Session()
    return _SESSION


def _http_get(url, headers, encoding="utf-8", retry=3, timeout=15, proxies=None):
    """GET。用共享 Session + 失败重试 3 次（间隔 1.5s，同 VPS 口径）。"""
    last = None
    s = _session()
    for i in range(retry):
        try:
            r = s.get(url, headers=headers, timeout=timeout, verify=False,
                      proxies=_PROXIES if proxies is None else proxies)
            r.raise_for_status()
            r.encoding = encoding
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise ShareSourceError("请求失败 %s … %s" % (url[:90], last))


def _date_range(start_iso, end_iso):
    d0 = datetime.datetime.strptime(start_iso, "%Y-%m-%d").date()
    d1 = datetime.datetime.strptime(end_iso, "%Y-%m-%d").date()
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += datetime.timedelta(days=1)
    return out


def fetch_szse_history(code, jjlb, start, end, verbose=False):
    """深交所官方：抓单只 [start, end] 全部登记日份额。

    `jjlb`：`'LOF'`（16xxxx）或 `'ETF'`（158/159xxx）。
    返回 `[(date_iso, pre_share_万份), ...]` 升序；空列表=该档期无数据。
    """
    out = {}
    page = 1
    while True:
        url = SZSE_URL.format(jjlb=jjlb, code=code, start=start, end=end,
                              page=page, rnd=random.random())
        j = json.loads(_http_get(url, SZSE_HEADERS))
        blk = j[0] if isinstance(j, list) and j else {}
        rows = blk.get("data") or []
        for r in rows:
            d = str(r.get("size_date") or "").strip()
            sid = str(r.get("fund_code") or "").strip()   # 🔴 当日行带尾随空格，必 strip
            sz = _num(r.get("current_size"))
            if d and sid == str(code) and sz is not None:
                out[_iso(d)] = sz
        pc = int((blk.get("metadata") or {}).get("pagecount") or 1)
        if verbose:
            print("    [%s/%s] page %d/%d → 累计 %d" % (code, jjlb, page, pc, len(out)))
        if page >= pc or not rows:
            break
        page += 1
        time.sleep(0.2)
    return sorted(out.items())


def fetch_szse_history_segmented(code, jjlb, start, end, verbose=False):
    """同 `fetch_szse_history`，但把超 6 个月的窗口切段合并（补长历史用）。"""
    d0 = datetime.datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.datetime.strptime(end, "%Y-%m-%d").date()
    segs, cur = [], d0
    while cur <= d1:
        seg_end = min(cur + datetime.timedelta(days=MAX_WINDOW_DAYS - 1), d1)
        segs.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = seg_end + datetime.timedelta(days=1)
    merged = {}
    for s, e in segs:
        for d, v in fetch_szse_history(code, jjlb, s, e, verbose=verbose):
            merged[d] = v
    return sorted(merged.items())


def fetch_sse_lof_day(date8, verbose=False):
    """上交所官方 LOFGMTJ：拉单个交易日的**沪市 LOF 全量**份额。

    返回 `{code: (date_iso, pre_share_万份)}`；非交易日/无数据返回 `{}`。
    🔴 日期参数名 = `SEARCH_DATE`（`20260924` 或 `2026-09-24` 均可）；无该日数据返空。
    """
    d8 = str(date8).replace("-", "")
    text = _http_get(SSE_URL.format(date8=d8), SSE_HEADERS)
    j = json.loads(text)
    res = j.get("result") or []
    out = {}
    for it in res:
        code = str(it.get("FUND_CODE") or "").strip()
        td = str(it.get("TRADE_DATE") or "").strip()
        v = _num(it.get("INTERNAL_VOL"))
        if not code or v is None:
            continue
        # 以接口返回的 TRADE_DATE 为准（避免请求日与登记日错位）
        out[code] = (_iso(td) if td else _iso(d8), v)
    if verbose:
        print("    [SSE %s] %d 只沪市 LOF" % (d8, len(out)))
    return out


def fetch_tencent_etf(code):
    """腾讯行情派生：**沪市 ETF 当日**份额（万份）。

    `field[72]` = 总份额（份）→ ÷1e4 = 万份；缺失时回退 `field[44](总市值,亿元)×1e4 ÷ field[3](现价)`。
    ⚠️ 腾讯只给实时快照 ⇒ **无法补历史**，只能采当天。
    """
    sym = ("sh" if str(code)[0] == "5" else "sz") + str(code)
    text = _http_get(TX_URL.format(sym=sym), TX_HEADERS, encoding="gbk", timeout=10)
    if "=" not in text:
        raise ShareSourceError("[%s] 腾讯无返回" % code)
    body = text.split("=", 1)[1].strip().rstrip(";").strip().strip('"')
    f = body.split("~")
    if len(f) < 73:
        raise ShareSourceError("[%s] 腾讯字段不足 (%d)" % (code, len(f)))
    total = _num(f[72])
    if not total or total <= 0:
        # 回退：总市值(亿元) × 1e4 ÷ 现价
        try:
            total = float(f[44]) * 1e4 / float(f[3])
        except Exception:
            raise ShareSourceError("[%s] 腾讯份额不可解析 (f72=%r)" % (code, f[72]))
    return round(total / 1e4, 4)      # 份 → 万份


def collect(codes, days=30, logger=None, interval=None, only=None):
    """批量采集（**无 DB 依赖**，供 arbTest 等同源复用）。

    返回 `(out, failed)`，其中
      `out`    = `[(code, date_iso, shares_万份, source), ...]` 升序；
      `failed` = `[(code, reason), ...]`。

    `codes`   ：6 位基金代码列表（自动按 `classify` 分派交易所）。
    `days`    ：补最近 N 天历史（沪 ETF 恒为当天）。
    `only`    ：只跑指定类别（'sz_lof'/'sz_etf'/'sh_lof'/'sh_etf'）。
    `interval`：深市每只最小间隔秒（缺省 3.0，照抄 VPS 2~4s 口径；**切勿 <2**）。
    `logger`  ：可选日志对象（`.info/.warning`）。
    """
    def log(msg, lvl="info"):
        if logger:
            getattr(logger, lvl, logger.info)(msg)

    iv = {"szse": 3.0 if interval is None else float(interval),
          "sse": 0.6 if interval is None else max(float(interval) / 3.0, 0.3),
          "tx": 0.15}

    picks = {"sz_lof": [], "sz_etf": [], "sh_lof": [], "sh_etf": []}
    for c in codes:
        k = classify(c)
        if k:
            picks[k].append(str(c))
    if only:
        want = set(only if isinstance(only, (list, tuple, set)) else [only])
        picks = {k: (v if k in want else []) for k, v in picks.items()}

    today = datetime.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - datetime.timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")

    out, failed = [], []

    # 深交所：深 LOF + 深 ETF（逐只，带日期窗）
    for kind, jjlb in (("sz_lof", "LOF"), ("sz_etf", "ETF")):
        consec = 0
        for code in picks[kind]:
            try:
                rows = fetch_szse_history_segmented(code, jjlb, start, end)
                consec = 0
                for d, v in rows:
                    out.append((code, d, v, SRC_SZSE))
            except Exception as e:
                consec += 1
                failed.append((code, str(e)[:110]))
                if consec >= _CIRCUIT_FAILS:
                    log("⛔ 深交所连续 %d 次失败，疑似限流，中断 %s" % (consec, kind), "warning")
                    break
            _sleep(iv["szse"])
        log("  %s: %d 只" % (kind, len(picks[kind])))

    # 上交所：沪 LOF（逐日全量）
    sh_lof = set(picks["sh_lof"])
    if sh_lof:
        consec = 0
        hit_days = 0
        for d in _date_range(start, end):
            try:
                day = fetch_sse_lof_day(d)
                consec = 0
            except Exception as e:
                consec += 1
                failed.append(("SSE:" + d, str(e)[:110]))
                if consec >= _CIRCUIT_FAILS:
                    log("⛔ 上交所连续 %d 次失败，中断" % consec, "warning")
                    break
                continue
            hit = 0
            for code in sh_lof:
                if code in day:
                    iso, v = day[code]
                    out.append((code, iso, v, SRC_SSE))
                    hit += 1
            if hit:
                hit_days += 1
            _sleep(iv["sse"])
        log("  沪 LOF: %d 只在 %d 个交易日有数据" % (len(sh_lof), hit_days))

    # 腾讯：沪 ETF（仅当天）
    consec = 0
    for code in picks["sh_etf"]:
        try:
            v = fetch_tencent_etf(code)
            consec = 0
            out.append((code, end, v, SRC_TX))
        except Exception as e:
            consec += 1
            failed.append((code, str(e)[:110]))
            if consec >= _CIRCUIT_FAILS:
                log("⛔ 腾讯连续 %d 次失败，中断" % consec, "warning")
                break
        _sleep(iv["tx"])
    log("  沪 ETF: %d 只" % len(picks["sh_etf"]))

    return out, failed


# ============================ 落库（ETFarb 专属） ============================
class ShareDirectFetcher:
    """交易所直采 → `unified_fund_history` 落库（shares/shares_added 幂等 upsert，自动求差）。"""

    def __init__(self, db=DB_PATH):
        self.db = db

    # ---- 标的清单 ----
    def universe(self, market=None, codes=None):
        """返回 `[(code, market), ...]`；`codes` 优先，其次按 `market` 过滤。"""
        c = sqlite3.connect(self.db)
        try:
            if codes:
                out = []
                for code in codes:
                    r = c.execute("SELECT jsl_market FROM fund_profile WHERE fund_code=?",
                                  (str(code),)).fetchone()
                    out.append((str(code), r[0] if r else None))
                return out
            sql = ("SELECT fund_code, jsl_market FROM fund_profile "
                   "ORDER BY fund_code")
            if market:
                return [(r[0], r[1]) for r in c.execute(
                    "SELECT fund_code, jsl_market FROM fund_profile WHERE jsl_market=? ORDER BY fund_code",
                    (market,))]
            return [(r[0], r[1]) for r in c.execute(sql)]
        finally:
            c.close()

    # ---- 落库 ----
    def save_rows(self, code, rows, source):
        """整批写 unified_fund_history（shares=pre_share 万份，shares_added=new_share 自动求差）。

        返回 `(written, new_filled)`。首行若无更早基准 ⇒ `shares_added` 留 NULL（设计如此）。
        🔴 分区写：in_arbtest=1 的 48 只份额归 arbTest，本模块（仅 ETFarb 调）跳过不碰。
        """
        if _is_in_arbtest(code):
            return 0, 0
        rows = sorted(rows)
        if not rows:
            return 0, 0
        c = sqlite3.connect(self.db)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        written = filled = 0
        try:
            for date_iso, pre in rows:
                if pre is None:
                    continue
                prev = c.execute(
                    "SELECT shares FROM unified_fund_history WHERE fund_code=? AND date<? "
                    "ORDER BY date DESC LIMIT 1", (code, date_iso)).fetchone()
                base = prev[0] if prev and prev[0] is not None else None
                new = round(pre - base, 4) if base is not None else None
                if new is not None:
                    filled += 1
                # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 shares 是谁写的"（凌晨批来源排查教训）
                c.execute(
                    """INSERT INTO unified_fund_history (fund_code, date, shares, shares_added, source, fetched_at, writer)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(fund_code, date) DO UPDATE SET
                         shares=excluded.shares, shares_added=excluded.shares_added,
                         source=excluded.source, fetched_at=excluded.fetched_at,
                         writer=excluded.writer""",
                    (code, date_iso, pre, new, source, ts, "etfarb:share_direct_fetcher"))
                written += 1
            c.commit()
        finally:
            c.close()
        return written, filled

    # ---- 主流程 ----
    def sync(self, days=30, market=None, codes=None, dry=False, verbose=True,
             interval=None, only=None):
        """交易所直采主流程。

        · 深 LOF / 深 ETF：逐只带日期窗（补 `days` 天历史）
        · 沪 LOF：逐日调上交所（一天一次拿全量）
        · 沪 ETF：腾讯实时（仅当天，无法补历史）

        `interval`：深市每只最小间隔秒（缺省 1.8，按 ±40% 抖动）；
                   沪LOF = interval/3、腾讯固定 0.15。
        `only`    ：只跑指定类别（'sz_lof'/'sz_etf'/'sh_lof'/'sh_etf'），缺省全跑。

        🔴 深交所反爬严格 ⇒ 连续失败 `_CIRCUIT_FAILS` 次即中断该类（下次再补）。

        返回统计 dict。
        """
        # 🔴 深市节奏照抄 VPS `SH_SZ_LOFshares.py`：脚本内 `random.uniform(2.0, 4.0)`，
        #    叠加请求耗时后 VPS 日志实测 5~8s/只。此处中心值取 3.0 ⇒ `_sleep(3.0)`
        #    抖动出 1.8~4.2s，再叠加请求耗时，与 VPS 同量级。**切勿再压到 2s 以下。**
        # 落库前确保 etfarb.db schema 存在（仅 ETFarb 运行时触发；
        # 被 arbTest 复用只调 collect()、不调 sync() ⇒ 不会触发此分支，导入零副作用）
        try:
            import db as _db
            _db.ensure_schema()
        except Exception:
            pass

        iv = {"szse": 3.0 if interval is None else float(interval),
              "sse": 0.6 if interval is None else max(float(interval) / 3.0, 0.3),
              "tx": 0.15}

        uni = self.universe(market=market, codes=codes)
        picks = {"sz_lof": [], "sz_etf": [], "sh_lof": [], "sh_etf": []}
        unknown = []
        for code, mk in uni:
            k = classify(code)
            if k:
                picks[k].append(code)
            else:
                unknown.append(code)
        if only:
            want = set(only if isinstance(only, (list, tuple, set)) else [only])
            picks = {k: (v if k in want else []) for k, v in picks.items()}

        today = datetime.date.today()
        end = today.strftime("%Y-%m-%d")
        start = (today - datetime.timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")

        st = {"start": start, "end": end, "dry": dry,
              "sz_lof": 0, "sz_etf": 0, "sh_lof": 0, "sh_etf": 0,
              "rows": 0, "new_filled": 0, "failed": [], "unknown": unknown}

        if verbose:
            print("交易所直采：%s ~ %s  深LOF=%d 深ETF=%d 沪LOF=%d 沪ETF=%d%s"
                  % (start, end, len(picks["sz_lof"]), len(picks["sz_etf"]),
                     len(picks["sh_lof"]), len(picks["sh_etf"]),
                     "  [DRY]" if dry else ""))

        # ---- 深交所：深 LOF + 深 ETF（逐只，带日期窗）----
        for kind, jjlb in (("sz_lof", "LOF"), ("sz_etf", "ETF")):
            todo = picks[kind]
            consec = 0
            for i, code in enumerate(todo, 1):
                try:
                    rows = fetch_szse_history_segmented(code, jjlb, start, end)
                    consec = 0
                    if not rows:
                        st["failed"].append((code, "空窗无数据"))
                        continue
                    if dry:
                        st[kind] += 1
                        st["rows"] += len(rows)
                        continue
                    n, nf = self.save_rows(code, rows, SRC_SZSE)
                    st[kind] += 1
                    st["rows"] += n
                    st["new_filled"] += nf
                except Exception as e:
                    consec += 1
                    st["failed"].append((code, str(e)[:120]))
                    if consec >= _CIRCUIT_FAILS:
                        st["failed"].append(("__circuit__",
                                             "%s 连续 %d 次失败，疑似深交所限流，中断该类" % (kind, consec)))
                        if verbose:
                            print("   ⛔ [%s] 连续 %d 次失败，中断（疑似深交所限流）" % (kind, consec))
                        break
                if verbose and (i % 25 == 0 or i == len(todo)):
                    print("   [%s] %d/%d …" % (kind, i, len(todo)))
                _sleep(iv["szse"])

        # ---- 上交所：沪 LOF（逐日，一天一次全量）----
        sh_lof = set(picks["sh_lof"])
        if sh_lof:
            ok_days = 0
            consec = 0
            for d in _date_range(start, end):
                try:
                    day = fetch_sse_lof_day(d)
                    consec = 0
                except Exception as e:
                    consec += 1
                    st["failed"].append(("SSE:" + d, str(e)[:120]))
                    if consec >= _CIRCUIT_FAILS:
                        st["failed"].append(("__circuit__", "SSE 连续 %d 次失败，中断" % consec))
                        break
                    continue
                hit = 0
                for code in sh_lof:
                    if code in day:
                        iso, wan = day[code]
                        hit += 1
                        if not dry:
                            n, nf = self.save_rows(code, [(iso, wan)], SRC_SSE)
                            st["rows"] += n
                            st["new_filled"] += nf
                if hit:
                    ok_days += 1
                    st["sh_lof"] = max(st["sh_lof"], hit)
                if dry:
                    st["rows"] += hit
                _sleep(iv["sse"])
            if verbose:
                print("   沪 LOF：%d 天有数据（覆盖 %d 只）" % (ok_days, st["sh_lof"]))

        # ---- 腾讯：沪 ETF（实时，仅当天）----
        tx_todo = picks["sh_etf"]
        consec = 0
        for i, code in enumerate(tx_todo, 1):
            try:
                wan = fetch_tencent_etf(code)
                consec = 0
                if dry:
                    st["sh_etf"] += 1
                    st["rows"] += 1
                    continue
                n, nf = self.save_rows(code, [(end, wan)], SRC_TX)
                st["sh_etf"] += 1
                st["rows"] += n
                st["new_filled"] += nf
            except Exception as e:
                consec += 1
                st["failed"].append((code, str(e)[:120]))
                if consec >= _CIRCUIT_FAILS:
                    st["failed"].append(("__circuit__", "腾讯连续 %d 次失败，中断" % consec))
                    break
            if verbose and (i % 40 == 0 or i == len(tx_todo)):
                print("   [sh_etf 腾讯] %d/%d …" % (i, len(tx_todo)))
            _sleep(iv["tx"])

        if verbose:
            print("✅ 直采完成：深LOF=%d 深ETF=%d 沪LOF=%d 沪ETF=%d 写行=%d 新增有值=%d 失败=%d"
                  % (st["sz_lof"], st["sz_etf"], st["sh_lof"], st["sh_etf"],
                     st["rows"], st["new_filled"], len(st["failed"])))
            if st["failed"][:8]:
                print("   ⚠️ 失败样本：", st["failed"][:8])
        return st

    # ---- 覆盖报告 ----
    def report(self, verbose=True):
        """打印各市场份额覆盖（最新日期分布 + 三列非空率）。"""
        c = sqlite3.connect(self.db)
        out = []
        out.append("date 覆盖（最近 12 个写入日）")
        for r in c.execute("SELECT date, COUNT(*), COUNT(DISTINCT fund_code) FROM unified_fund_history "
                           "GROUP BY date ORDER BY date DESC LIMIT 12"):
            out.append("   %s  %4d 行  %3d 只" % (r[0], r[1], r[2]))
        out.append("")
        out.append("按市场（最新一行是否含份额 / 新增）")
        for mk in ("A", "E", "L", "C", "WATCH"):
            rows = list(c.execute("""
                SELECT p.fund_code,
                       (SELECT shares FROM unified_fund_history f WHERE f.fund_code=p.fund_code
                        ORDER BY date DESC LIMIT 1),
                       (SELECT shares_added FROM unified_fund_history f WHERE f.fund_code=p.fund_code
                        ORDER BY date DESC LIMIT 1),
                       (SELECT MAX(date) FROM unified_fund_history f WHERE f.fund_code=p.fund_code)
                FROM fund_profile p WHERE p.jsl_market=?""", (mk,)))
            if not rows:
                continue
            n = len(rows)
            ps = sum(1 for r in rows if r[1] is not None)
            ns = sum(1 for r in rows if r[2] is not None)
            dts = sorted(set(r[3] for r in rows if r[3]))
            out.append("   %-5s 共%3d  份额%3d 新增%3d  日期 %s ~ %s"
                       % (mk, n, ps, ns, dts[0] if dts else "—", dts[-1] if dts else "—"))
        c.close()
        if verbose:
            print("\n".join(out))
        return "\n".join(out)


# ============================ CLI ============================
def main():
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      line_buffering=True)
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="ETFarb 场内份额 —— 交易所直采")
    ap.add_argument("--sync", action="store_true", help="执行采集（深沪 LOF/ETF）")
    ap.add_argument("--days", type=int, default=30, help="补最近 N 天（默认 30）")
    ap.add_argument("--market", help="限定市场 A/E/L/C/WATCH")
    ap.add_argument("--codes", help="限定标的，逗号分隔")
    ap.add_argument("--dry", action="store_true", help="只抓不落库")
    ap.add_argument("--interval", type=float, default=None,
                    help="深市每只最小间隔秒（缺省 3.0，照抄 VPS 2~4s 口径；切勿 <2）")
    ap.add_argument("--only", help="只跑类别，逗号分隔：sz_lof,sz_etf,sh_lof,sh_etf")
    ap.add_argument("--report", action="store_true", help="打印份额覆盖报告")
    a = ap.parse_args()
    f = ShareDirectFetcher()
    codes = [x.strip() for x in a.codes.split(",")] if a.codes else None
    only = [x.strip() for x in a.only.split(",")] if a.only else None
    if a.sync:
        f.sync(days=a.days, market=a.market, codes=codes, dry=a.dry,
               interval=a.interval, only=only)
    if a.report or not a.sync:
        f.report()


if __name__ == "__main__":
    main()
