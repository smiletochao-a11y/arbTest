# -*- coding: utf-8 -*-
"""深市 ETF 份额历史采集（深交所官方 API，2026-09-27 立项）。

背景
----
`core/share_fetcher.py` 走东财 `f84`（单日快照），**只取最新一天、无历史**；
且东京 VPS 上东财 502 ⇒ ETF 份额历史全断（详见 .workbuddy/memory/2026-09-26.md）。
本模块走**深交所官方接口**，直接取**按日历史**，是份额历史的正源。

接口（2026-09-26 逆向页面 `chnlcode`/`CATALOGID` 挖出，09-27 本机实测）
--------------------------------------------------------------------
```
https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON
  &CATALOGID=scsj_fund_jjgm     # 基金规模
  &jjlb=ETF                     # 类别=ETF（LOF 走别的 jjlb）
  &txtDm=<基金代码>              # 单码；多码逗号/竖线实测**不生效**（返回 0）⇒ 一只一只查
  &txtStart=YYYY-MM-DD&txtEnd=YYYY-MM-DD   # 🔴 日期窗上限约 6 个月，更早须分段
  &PAGENO=<页码>                # 🔴 分页参数**必须 PAGENO**（大写）；PAGESIZE 完全无效，固定 20 行/页
```
返回字段：`size_date / fund_code / security_short_name / current_size`

🔴 **口径红线（实测逐位对齐，2026-09-26 东哥 xlsx 验证）**
  · `current_size` 单位 = **万份**，与 `unified_fund_history.shares` **同口径**，可直接写。
  · **`size_date` = 份额登记日**。VPS 06:00 采集拿到的是 **T-1 登记值** ⇒ 回补时按
    `size_date` 原样写，不要偏移日期（对齐 docs 011_2「日期对齐」§Bug3）。
  · 159687 全历史 07-06~09-24 抓 59 行，09-24 = 168,342.86 万份，与东哥 xlsx 逐位吻合。

⚠️ 本模块**只读深交所、只写 unified_fund_history**（单库方案，shares=pre_share 万份，
shares_added=new_share 自动求差），不碰 VPS 脚本（红线：VPS 生产程序严禁 AI 私改）。
"""
import datetime
import json
import os
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
    """分区写红线：in_arbtest=1 的 48 只份额归 arbTest，ETFarb 跳过写。"""
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

SZSE_URL = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            "&CATALOGID=scsj_fund_jjgm&jjlb=ETF"
            "&txtDm={code}&txtStart={start}&txtEnd={end}&PAGENO={page}")
_REFERER = "https://www.szse.cn/market/fund/volume/etf/index.html"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 日期窗口上限约 6 个月（页面 JS 的 dateRange('all',...,'6m') 印证）
MAX_WINDOW_DAYS = 150


class SzseShareMissing(Exception):
    """深交所份额取不到。绝不兜底。"""
    pass


def _conn():
    return sqlite3.connect(DB_PATH)


class SzseShareFetcher:
    """深市 ETF 份额历史采集（官方 API）。"""

    def __init__(self, db=DB_PATH):
        self.db = db

    # ---- 低层取数 ----
    def _get(self, url, retry=3):
        import requests
        last = None
        for i in range(retry):
            try:
                r = requests.get(url, headers={"User-Agent": _UA, "Referer": _REFERER},
                                 timeout=15)
                r.raise_for_status()
                return r.text
            except Exception as e:
                last = e
                time.sleep(0.6 * (i + 1))
        raise SzseShareMissing("深交所份额请求失败: %s" % last)

    @staticmethod
    def _num(s):
        """'168,342.86' → 168342.86；空/异常 → None。"""
        if s is None:
            return None
        t = str(s).strip().replace(",", "")
        if t in ("", "-", "—"):
            return None
        try:
            return float(t)
        except ValueError:
            return None

    # ---- 单只全历史 ----
    def fetch_history(self, code, start, end, verbose=False):
        """抓单只 [start, end] 全部登记日份额。

        返回 `[(date_iso, pre_share_万份), ...]`，按日期升序；空则抛 `SzseShareMissing`。
        `date_iso` 形如 `2026-09-24`（与 `unified_fund_history.date` 口径一致）。
        """
        out = []
        page = 1
        while True:
            url = SZSE_URL.format(code=code, start=start, end=end, page=page)
            j = json.loads(self._get(url))
            blk = j[0] if isinstance(j, list) and j else {}
            meta = blk.get("metadata") or {}
            rows = blk.get("data") or []
            for r in rows:
                d = str(r.get("size_date") or "").strip()
                sid = str(r.get("fund_code") or "").strip()   # 🔴 当日行带尾随空格，必 strip
                sz = self._num(r.get("current_size"))
                if not d or sid != str(code) or sz is None:
                    continue
                out.append((d, sz))
            pc = int(meta.get("pagecount") or 1)
            if verbose:
                print("    [%s] page %d/%d → 累计 %d 行" % (code, page, pc, len(out)))
            if page >= pc or not rows:
                break
            page += 1
            time.sleep(0.2)
        if not out:
            raise SzseShareMissing("[%s] %s~%s 深交所无份额数据" % (code, start, end))
        out.sort(key=lambda x: x[0])
        return out

    # ---- 分段（日期窗 ≤6 个月）----
    def fetch_history_segmented(self, code, start, end, verbose=False):
        """把 [start,end] 切成 ≤150 天的窗口逐段抓，合并去重升序。"""
        d0 = datetime.datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.datetime.strptime(end, "%Y-%m-%d").date()
        segs, cur = [], d0
        while cur <= d1:
            seg_end = min(cur + datetime.timedelta(days=MAX_WINDOW_DAYS - 1), d1)
            segs.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
            cur = seg_end + datetime.timedelta(days=1)
        merged = {}
        for s, e in segs:
            for d, v in self.fetch_history(code, s, e, verbose=verbose):
                merged[d] = v
        rows = sorted(merged.items(), key=lambda x: x[0])
        if not rows:
            raise SzseShareMissing("[%s] %s~%s 分段抓取后仍无数据" % (code, start, end))
        return rows

    # ---- 落库 ----
    def save_history(self, code, rows, source="szse_jjgm"):
        """整批写入 unified_fund_history（shares=pre_share 万份，shares_added=new_share 求差）。

        逐行按日期升序写入，保证「新增」用前一日（同批或库内）值求差；
        首行若库内无更早基准 ⇒ `shares_added` 留 NULL（首日无基准，设计如此）。
        🔴 分区写：in_arbtest=1 的 48 只份额归 arbTest，本模块跳过不碰。
        返回 `{'written': n, 'new_filled': m, 'first_date':..., 'last_date':...}`。
        """
        if _is_in_arbtest(code):
            return {"written": 0, "new_filled": 0,
                    "first_date": rows[0][0] if rows else None,
                    "last_date": rows[-1][0] if rows else None}
        c = _conn()
        written = new_filled = 0
        for date_iso, pre in rows:
            prev = c.execute(
                "SELECT shares FROM unified_fund_history WHERE fund_code=? AND date<? "
                "ORDER BY date DESC LIMIT 1", (code, date_iso)).fetchone()
            # 注意：本行可能已存在，需先看本行旧值（避免自比）
            cur_row = c.execute(
                "SELECT shares FROM unified_fund_history WHERE fund_code=? AND date=?",
                (code, date_iso)).fetchone()
            base = prev[0] if prev and prev[0] is not None else None
            new = (pre - base) if base is not None else None
            if new is not None:
                new_filled += 1
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 shares 是谁写的"（凌晨批来源排查教训）
            c.execute(
                """INSERT INTO unified_fund_history (fund_code, date, shares, shares_added, source, fetched_at, writer)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(fund_code, date) DO UPDATE SET
                     shares=excluded.shares,
                     shares_added=excluded.shares_added,
                     source=excluded.source, fetched_at=excluded.fetched_at,
                     writer=excluded.writer""",
                (code, date_iso, pre, new, source,
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "etfarb:szse_share_fetcher"))
            written += 1
        c.commit()
        c.close()
        return {"written": written, "new_filled": new_filled,
                "first_date": rows[0][0], "last_date": rows[-1][0]}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="深市 ETF 份额历史采集（深交所官方 API）")
    ap.add_argument("codes", nargs="*", help="基金代码（缺省=WATCH 池深市四只）")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（缺省 6 个月前）")
    ap.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（缺省今天）")
    ap.add_argument("--dry-run", action="store_true", help="只抓不写库")
    a = ap.parse_args()

    codes = a.codes or ["159687", "159366", "159303", "159513"]
    end = a.end or datetime.date.today().strftime("%Y-%m-%d")
    start = a.start or (datetime.date.today() - datetime.timedelta(days=MAX_WINDOW_DAYS - 1)
                        ).strftime("%Y-%m-%d")
    f = SzseShareFetcher()
    for code in codes:
        try:
            rows = f.fetch_history_segmented(code, start, end, verbose=False)
            print("[%s] 抓 %d 行 %s ~ %s  最新=%.2f 万份"
                  % (code, len(rows), rows[0][0], rows[-1][0], rows[-1][1]))
            if not a.dry_run:
                r = f.save_history(code, rows)
                print("    写入 %d 行，新增有值 %d 行" % (r["written"], r["new_filled"]))
        except Exception as e:
            print("[%s] ❌ %s" % (code, e))


if __name__ == "__main__":
    main()
