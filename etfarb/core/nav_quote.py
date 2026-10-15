# -*- coding: utf-8 -*-
"""core/nav_quote.py — 基金「最新已公布净值」实时口（腾讯 `jj` 前缀）
================================================================
为跨境 ETF 看板的「净值 / 净值日 / 净值溢价」三列提供与**集思录同口径**的输入。

    https://qt.gtimg.cn/q=jj<code>[,jj<code>…]        # gbk，**多码一次**
    v_jj159941="159941~广发纳指100ETF~0.0000~0.0000~~1.4608~5.8432~-0.0411~2026-09-16~"
                                                        [5]      [6]     [7]      [8]
       [5] 单位净值   [6] 累计净值   [7] 净值日涨跌%   [8] **净值日期**（YYYY-MM-DD）

🔴 为什么需要它（docs/40 §七·17）
---------------------------------
看板原先的净值取 **PCF 头部 `<NAV>`**，那是**净值基准日 D** 的官方单位净值，
而集思录的「净值」列取的是「**最新已公布净值**」。两者**天生差一天** —— 且是**时点相关**的：

| 时刻 | 集思录 / `jj[8]` | PCF `<NAV>` = D |
|---|---|---|
| 盘中（亚洲，T 日） | **T-1**（今日尚未公布） | **T-1** ✅ 巧合一致 |
| 盘后（亚洲，当晚公布后） | **T** | T-1 ❌ 差一天 |
| 欧美 | 同规律，另行滞后一档 | 同规律 |

⇒ **盘中两边本来就一致；只有盘后才分叉**（东哥 2026-09-17 指出）。
所以修法不需要任何「时点判断」，只要取**最新已公布**，两边就自动同口径。

🔴 与 `nav_fetcher.py` 的分工
-----------------------------
| 模块 | 源 | 用途 | 落库 |
|---|---|---|---|
| **本模块 `nav_quote`** | 腾讯 `jj`（行情口） | **最新一条**净值 + **净值日期**，供看板**实时上屏** | ❌ 不落库 |
| `nav_fetcher` | 东财 `f10/lsjz` | 历史序列，增量写 `unified_fund_history(nav)` | ✅ |

⚠️ 历史回补将来可换腾讯 `year_all`（`stockjs.finance.qq.com/fundUnitNavAll/data/year_all/<code>.js`，
gb2312，一次给全历史，213ms/只、全池 52s）—— 但它**最新只到 T-1**，**不能当「最新净值」用**。

🔴 成本（2026-09-17 实测）
--------------------------
**60 码/请求**，全池 246 只 = **5 请求 / 约 1.1 秒**。看板 30 秒一轮也仅 ~10 请求/分钟，
远低于任何风控阈值。**不逐只发请求**（那是东财口径，18.4s/只）。
"""
import os
import re
import ssl
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

QT_URL = "https://qt.gtimg.cn/q="
_CHUNK = 60                    # 单请求代码数（URL 长度安全上限）
TTL = 60                       # 秒。净值一天只公布一次，缓存 60s 完全无感

# 进程级缓存：code -> (ts, {nav, nav_date, acc, chg_pct})
_CACHE = {}


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _iso(d):
    """'2026-09-16' / '20260916' → '2026-09-16'；非法返回 None。"""
    s = str(d or "").strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return None


class NavQuoteFetcher:
    """腾讯 `jj` 批量取「最新已公布净值」。取不到就是取不到，**绝不兜底**。"""

    def __init__(self, ttl=TTL):
        self.ttl = ttl

    # ------------------------------------------------------------------
    def batch_latest(self, codes):
        """`{code: {'nav':float, 'nav_date':'YYYY-MM-DD', 'acc':float, 'chg_pct':float}}`。

        只返回**本次真正取到**的码；未命中的码不在结果里（调用方自行决定回退策略）。
        进程内 60 秒缓存，重复调用不重复打请求。
        """
        codes = [str(c).strip() for c in codes if str(c).strip()]
        if not codes:
            return {}
        now = time.time()
        out, need = {}, []
        for c in codes:
            hit = _CACHE.get(c)
            if hit and (now - hit[0]) < self.ttl:
                out[c] = hit[1]
            else:
                need.append(c)

        for i in range(0, len(need), _CHUNK):
            batch = need[i:i + _CHUNK]
            url = QT_URL + ",".join("jj" + c for c in batch)
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": _UA, "Referer": "https://finance.qq.com/"})
                raw = urllib.request.urlopen(req, timeout=20, context=_CTX).read().decode(
                    "gbk", "ignore")
            except Exception:
                continue                    # 整批失败 ⇒ 这批判为未命中，不伪造
            for line in raw.strip().split(";"):
                m = re.match(r'v_jj(\d{6})="(.*)"$', line.strip())
                if not m:
                    continue
                p = m.group(2).split("~")
                if len(p) < 9:
                    continue
                nav = _num(p[5])
                nd = _iso(p[8])
                if nav is None or not nd:
                    continue                # 无净值或无日期 ⇒ 视为未命中
                info = {"nav": nav, "nav_date": nd,
                        "acc": _num(p[6]), "chg_pct": _num(p[7])}
                out[m.group(1)] = info
                _CACHE[m.group(1)] = (now, info)
        return out


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    args = sys.argv[1:] or ["159941", "513500", "513000", "159366"]
    f = NavQuoteFetcher(ttl=0)
    t0 = time.time()
    r = f.batch_latest(args)
    print("命中 %d / %d   用时 %.2fs" % (len(r), len(args), time.time() - t0))
    for c in args:
        v = r.get(c)
        print("  %s  %s" % (c, ("nav=%s  date=%s  acc=%s" % (v["nav"], v["nav_date"], v["acc"]))
                            if v else "—（未命中）"))
