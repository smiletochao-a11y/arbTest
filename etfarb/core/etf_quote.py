"""ETFarb ETF 本体行情：二级市场日收盘价 → unified_fund_history.price。

为什么需要（2026-09-16 补）：
  历史明细页的折溢价列取 `unified_fund_history.price[D]`（`backend/app.py:_iopv_detail`），
  而该列此前**只被实时价回写「最新一行」**（`app.py:_get_etf_price`），历史日期
  全靠 `iopv_point` 的盘中采样兜。新接入的 ETF（159366）两者皆无 →
  折溢价列整列空白。本模块补的正是这一环：**按日回填 ETF 二级市场收盘价**。

口径（与 IOPV 引擎一致，别混）：
  · 用**不复权**日K = 真实成交价，与 `iopv_point` 的实时价同源（腾讯）。
  · **只写 price 列，绝不碰 nav** —— nav 归 `nav_fetcher.py`（东财净值）。
    故用 `upsert_fund_history`（只动 price 列）；若整行替换会把 nav（以及
    arbTest 在重叠 48 只上写的派生列）清成 NULL —— 这是踩过的坑。
  · 与 `hk_quote` 的分工：hk_quote 管**篮子里的港股**，本模块管 **ETF 自己**。

腾讯接口：`web.ifzq.gtimg.cn/appstock/app/kline/kline?param=sz159366,day,,,400`
  返回 `data[sym]['day'] = [[date, open, close, high, low, volume], ...]`（不复权）
"""
import os
import sys
import json
import ssl
import sqlite3
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.db import upsert_fund_history  # noqa: E402

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_KLINE_TPL = ("https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
              "?param={sym},day,,,{count}")

SOURCE = "tencent_etf"


def to_symbol(code):
    """'159366' -> 'sz159366'；'513590' -> 'sh513590'；已带 sh/sz 前缀则原样返回。"""
    c = str(code or "").strip().lower()
    if c[:2] in ("sh", "sz"):
        return c
    c = c.split(".")[0]
    return ("sz" if c[:1] in "0123" else "sh") + c


class ETFQuoteFetcher:
    def __init__(self):
        self._cache = {}

    def fetch_daily_closes(self, code, days=400):
        """不复权日收盘 → {iso_date: close}。取不到返回 {}（不兜底造数）。"""
        sym = to_symbol(code)
        if sym in self._cache:
            return self._cache[sym]
        url = _KLINE_TPL.format(sym=sym, count=int(days))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        raw = urllib.request.urlopen(req, timeout=25,
                                     context=_CTX).read().decode("utf-8", "ignore")
        j = json.loads(raw)
        rows = ((j.get("data") or {}).get(sym) or {}).get("day") or []
        out = {}
        for r in rows:
            try:
                out[str(r[0])] = float(r[2])      # [date, open, close, high, low, vol]
            except (IndexError, TypeError, ValueError):
                continue
        if out:
            self._cache[sym] = out
        return out

    def backfill_price(self, code, days=400, start=None, end=None):
        """把 ETF 二级市场日收盘写进 `unified_fund_history.price`（只动 price 列）。
        返回写入行数。

        `start` / `end` 为 ISO 日期（含），用于只回填感兴趣的区间。
        """
        code = str(code).split(".")[0]
        closes = self.fetch_daily_closes(code, days=days)
        if not closes:
            return 0
        rows = [(d, v) for d, v in sorted(closes.items())
                if (start is None or d >= start) and (end is None or d <= end)]
        if not rows:
            return 0
        n = 0
        for d, v in rows:
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 price 是谁写的"（凌晨批来源排查教训）
            upsert_fund_history(code, d, {"price": v}, source=SOURCE, writer="etfarb:etf_quote")
            n += 1
        return n


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "159366"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    f = ETFQuoteFetcher()
    cl = f.fetch_daily_closes(code, days=days)
    print("%s(%s): 取到 %d 根日K" % (code, to_symbol(code), len(cl)))
    if cl:
        ks = sorted(cl)
        print("  区间 %s ~ %s" % (ks[0], ks[-1]))
        for k in ks[-5:]:
            print("    %s  %s" % (k, cl[k]))
        n = f.backfill_price(code, days=days)
        print("  回填 unified_fund_history.price: %d 行" % n)
