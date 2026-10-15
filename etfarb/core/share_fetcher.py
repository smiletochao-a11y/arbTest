# -*- coding: utf-8 -*-
"""跨境 ETF 看板 —— 盘前份额采集（P1，2026-09-17 立项，docs/40）。

数据源（实测钉死）
------------------
东财 `push2delay.eastmoney.com/api/qt/stock/get?secid=<mkt>.<code>&fields=f84`
  · `f84` = **基金总份额（份）**，÷ 10000 = 万份。
  · 与集思录「盘前份额」**逐位吻合**：513090 `f84=9,885,988,096` → 988,598.8 万份
    vs 集思录 988,599 万份；159366 312,333,072 → 31,233.3 vs 31,233；
    513000 1,062,411,408 → 106,241.1 vs 106,241；513130 同理。
  · ⚠️ **不能用 `ulist.np` 批量接口的 `f84`** —— 同名编号语义不同
    （513090 返回 90,670,693，甚至出现负数），已验证是**错的数据**。
    故走**单只** `stock/get`：份额是**每日一次**的量（盘中不变），
    246 次/日 请求频率极低，符合 docs/40 的东财克制红线。
  · `secid` 前缀：沪市 `1.` / 深市 `0.`。

「盘前新增」
------------
= 今日盘前份额 − 上一有记录日期的盘前份额。上线首日无基准 ⇒ **留空**（东哥已接受）。
"""
import datetime
import io
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

EM_URL = ("https://push2delay.eastmoney.com/api/qt/stock/get"
          "?secid={secid}&fields=f12,f14,f84")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class ShareMissing(Exception):
    """份额取不到。绝不兜底。"""
    pass


def _secid(code):
    return ("1." if str(code)[:1] == "5" else "0.") + str(code)


def _conn():
    return sqlite3.connect(DB_PATH)


def _today8():
    return datetime.datetime.now().strftime("%Y%m%d")


class ShareFetcher:
    """盘前份额采集与落库。日频，非盘中刷新项。"""

    def __init__(self, db=DB_PATH):
        self.db = db

    # ---- 取数 ----
    def _get(self, url, retry=3):
        import requests
        last = None
        for i in range(retry):
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
                    timeout=8)
                r.raise_for_status()
                return r.text
            except Exception as e:      # 瞬时抖动重试；仍失败则暴露
                last = e
                time.sleep(0.6 * (i + 1))
        raise ShareMissing("东财份额请求失败: %s" % last)

    def fetch_one(self, code):
        """单只份额 → 万份（float）。取不到抛 `ShareMissing`（不兜底、不填 0）。"""
        d = json.loads(self._get(EM_URL.format(secid=_secid(code)))).get("data")
        if not d:
            raise ShareMissing("[%s] 东财无返回" % code)
        f84 = d.get("f84")
        if f84 in (None, "", 0):
            raise ShareMissing("[%s] 东财 f84 无值（%r）" % (code, f84))
        try:
            return float(f84) / 10000.0          # 份 → 万份
        except (TypeError, ValueError):
            raise ShareMissing("[%s] f84 解析失败: %r" % (code, f84))

    # ---- 落库 ----
    def _prev_share(self, c, code, date):
        r = c.execute(
            "SELECT shares FROM unified_fund_history WHERE fund_code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
        return r[0] if r and r[0] is not None else None

    def save(self, code, date, pre_share, source="eastmoney_f84"):
        """写一行到 unified_fund_history（shares=pre_share, shares_added=新求差）。
        `shares_added` 自动按上一有记录日期求差（无基准则留 NULL）。
        🔴 分区写：in_arbtest=1 的 48 只份额归 arbTest，ETFarb 跳过写。"""
        if _is_in_arbtest(code):
            return None
        c = _conn()
        prev = self._prev_share(c, code, date)
        c.close()
        new = (pre_share - prev) if prev is not None else None
        _db.upsert_fund_history(
            code, date,
            {"shares": pre_share, "shares_added": new, "source": source,
             "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 shares 是谁写的"
            writer="etfarb:share_fetcher")
        return {"fund_code": code, "date": date, "pre_share": pre_share, "new_share": new}

    def fetch_daily(self, codes=None, date=None, sleep=0.15, verbose=True):
        """采集一批标的的当日份额。返回 `{'ok': [...], 'failed': [(code, err)]}`。"""
        date = str(date or _today8())
        if codes is None:
            c = _conn()
            codes = [r[0] for r in c.execute("SELECT fund_code FROM fund_profile")]
            c.close()
        ok, failed = [], []
        for i, code in enumerate(codes, 1):
            try:
                v = self.fetch_one(code)
                ok.append(self.save(code, date, v))
                if verbose and (i % 25 == 0 or i == len(codes)):
                    print("  [%d/%d] 份额采集… 最近 %s = %.1f 万份"
                          % (i, len(codes), code, v))
            except Exception as e:
                failed.append((code, str(e)))
                if verbose:
                    print("  [%d/%d] %s ❌ %s" % (i, len(codes), code, e))
            if sleep:
                time.sleep(sleep)
        return {"ok": ok, "failed": failed, "date": date}

    # ---- 首日初始化：从集思录快照导入 ----
    def import_jsl(self, date=None):
        """把集思录两表快照的「盘前份额 / 盘前新增」导入 `unified_fund_history`（shares/shares_added）。

        ⚠️ 集思录「盘前份额」= 该日**开盘前**份额（= 前一交易日日终登记值），
           与东财 `f84` 当日采集**同口径**（已实测逐位吻合）⇒ 可直接对同一天 `date` 写。
        东哥手工喂的历史份额也走这里（格式同集思录两列）。
        """
        import etf_universe as U
        date = str(date or _today8())
        c = _conn()
        n = 0
        for r in U.load_pool():
            code = r["code"]
            if _is_in_arbtest(code):
                continue  # 分区写：48 只份额归 arbTest
            raw = (r.get("pre_share") or "").strip()
            if not raw or raw in ("-", "—"):
                continue
            try:
                pre = float(raw)
            except ValueError:
                continue
            raw_new = (r.get("pre_new") or "").strip()
            try:
                new = float(raw_new) if raw_new not in ("", "-", "—") else None
            except ValueError:
                new = None
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 shares 是谁写的"（jsl 快照批量路径）
            c.execute(
                """INSERT INTO unified_fund_history (fund_code, date, shares, shares_added, source, fetched_at, writer)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(fund_code, date) DO UPDATE SET
                     shares=excluded.shares, shares_added=excluded.shares_added,
                     source=excluded.source, fetched_at=excluded.fetched_at,
                     writer=excluded.writer""",
                (code, date, pre, new, "jsl_snapshot",
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "etfarb:share_fetcher.jsl_snapshot"))
            n += 1
        c.commit()
        c.close()
        return n

    # ---- 读取 ----
    def latest(self, codes=None, date=None):
        """返回 `{code: {'date':..., 'pre_share':..., 'new_share':...}}`（各取最新一行）。
        读 unified_fund_history(shares AS pre_share, shares_added AS new_share)。"""
        c = _conn()
        sql = ("SELECT fund_code, date, shares, shares_added FROM unified_fund_history "
               "WHERE (fund_code, date) IN (SELECT fund_code, MAX(date) FROM unified_fund_history "
               "GROUP BY fund_code)")
        if date:
            sql = ("SELECT fund_code, date, shares, shares_added FROM unified_fund_history WHERE date=?")
        if codes:
            ph = ",".join("?" * len(codes))
            sql = ("SELECT fund_code, date, shares, shares_added FROM unified_fund_history "
                   "WHERE fund_code IN (%s) AND date=(SELECT MAX(date) FROM unified_fund_history f2 "
                   "WHERE f2.fund_code=unified_fund_history.fund_code)" % ph)
            rows = c.execute(sql, list(codes)).fetchall()
        elif date:
            rows = c.execute(sql, (str(date),)).fetchall()
        else:
            rows = c.execute(sql).fetchall()
        c.close()
        return {r[0]: {"date": r[1], "pre_share": r[2], "new_share": r[3]} for r in rows}


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(description="跨境 ETF 看板 —— 盘前份额采集")
    ap.add_argument("--import-jsl", metavar="DATE", nargs="?", const="",
                    help="从集思录快照导入份额（可给日期 YYYYMMDD，缺省今天）")
    ap.add_argument("--fetch", action="store_true", help="从东财采集当日份额（全池）")
    ap.add_argument("--code", help="只采/看单只")
    ap.add_argument("--show", action="store_true", help="打印库内份额快照")
    a = ap.parse_args()
    f = ShareFetcher()
    if a.import_jsl is not None:
        n = f.import_jsl(a.import_jsl or None)
        print("已从集思录快照导入 %d 只份额" % n)
    if a.fetch:
        codes = [a.code] if a.code else None
        r = f.fetch_daily(codes=codes)
        print("采集完成 date=%s ok=%d failed=%d" % (r["date"], len(r["ok"]), len(r["failed"])))
    if a.show:
        d = f.latest([a.code] if a.code else None)
        for k in sorted(d):
            v = d[k]
            print("  %s  %s  份额=%.1f 万份  新增=%s"
                  % (k, v["date"], v["pre_share"] or 0,
                     ("%.1f" % v["new_share"]) if v["new_share"] is not None else "—"))


if __name__ == "__main__":
    main()
