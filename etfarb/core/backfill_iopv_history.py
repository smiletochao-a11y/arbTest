"""ETFarb 历史回填：给已接入的 ETF 补齐一段历史的 PCF / 汇率 / 行情 / 校准。

用法：
    python core/backfill_iopv_history.py --code 159366 --start 2026-08-17
    python core/backfill_iopv_history.py --code 159366 --days 30
    python core/backfill_iopv_history.py --code 159366 --only calib --force

📌 为什么要一个脚本：历史明细页（`app.py:_iopv_detail`）**一行要凑齐 6 个因子** ——

    mkt(ETF 二级市场价 D)  iopv(IOPV_close D)  nav_est(D+1 公告官方 NAV)
    settle(D 结算汇率)     mid(D 中间价)      篮子校准(iopv_verify D)

少任何一个，那一行就是空的。本脚本把这 6 条链一次补齐，全部幂等。

流程（任一步失败不影响已完成的部分，可直接重跑）：
    ① PCF      逐日 `fetch_and_store`（非交易日 / 未发布 → 跳过，**绝不兜底**）
    ② 汇率     最新中间价 `fetch_chinamoney`；结算汇率用参考汇率中值反推
               （键位按 `pcf_source` 定：深市 `_SZ` / 沪市 `_SH`）
    ③ 行情     港股成分 `prefetch` 预热（每只 2 次请求，替代「逐日 × 逐只」上万次）
               + ETF 本体日K → `fund_nav.price`
    ④ 校准     `verify_all_pending` + `calc_all`

🔴 **prefetch 的缓存挂在 `HKQuoteFetcher` 实例上**，而 `IOPVVerifier` / `IOPVCloseCalc`
   各自 new 了自己的 `IOPVCalculator`（→ 各自的 `hk`）。故本脚本**显式共享同一个
   fetcher 实例**（`v.calc.hk = HK`），否则预热白做、退化成逐日逐只请求。
"""
import argparse
import datetime
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.db import DB_PATH                              # noqa: E402
from core.etf_quote import ETFQuoteFetcher               # noqa: E402
from core.fx_provider import FXProvider                  # noqa: E402
from core.hk_quote import HKQuoteFetcher                 # noqa: E402
from core.iopv_calc import IOPVCloseCalc, IOPVVerifier   # noqa: E402
from core.iopv_pcf import PCFUnavailable, PCFFetcher     # noqa: E402

LINE = "=" * 74


def _weekdays(d1, d2):
    out, d = [], d1
    while d <= d2:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _hk_codes(code):
    c = sqlite3.connect(DB_PATH)
    codes = [r[0] for r in c.execute(
        "SELECT DISTINCT stock_code FROM pcf_component "
        "WHERE fund_code=? AND stock_code<>'159900'", (code,))]
    c.close()
    return sorted(set(codes))


def _market_of(code):
    c = sqlite3.connect(DB_PATH)
    row = c.execute("SELECT pcf_source FROM fund_iopv_config WHERE fund_code=?",
                    (code,)).fetchone()
    c.close()
    src = (row[0] if row else "") or ""
    return src, ("SZ" if src.lower() == "szse_txt" else "SH")


def main():
    ap = argparse.ArgumentParser(description="ETFarb 历史回填（PCF / 汇率 / 行情 / 校准）")
    ap.add_argument("--code", default="159366")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD（含）")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD（含），缺省=今天")
    ap.add_argument("--days", type=int, default=30, help="未给 --start 时向前回溯的天数")
    ap.add_argument("--force", action="store_true", help="校准 / 收盘 IOPV 全部重算")
    ap.add_argument("--only", default=None, choices=["pcf", "fx", "quote", "calib"],
                    help="只跑某一步")
    args = ap.parse_args()

    code = args.code
    end_d = datetime.date.fromisoformat(args.end) if args.end else datetime.date.today()
    start_d = (datetime.date.fromisoformat(args.start) if args.start
               else end_d - datetime.timedelta(days=args.days))
    s_iso, e_iso = start_d.isoformat(), end_d.isoformat()

    src, market = _market_of(code)
    if not src:
        print("!! fund_iopv_config 里没有 %s —— 先跑 core/iopv_pcf.py --init" % code)
        return 1

    print(LINE)
    print("历史回填  %s   %s ~ %s   pcf_source=%s   结算键位=_%s"
          % (code, s_iso, e_iso, src, market))
    print(LINE)

    HK = None

    # ① PCF ----------------------------------------------------------------
    if args.only in (None, "pcf"):
        f = PCFFetcher()
        c = sqlite3.connect(DB_PATH)
        have = {r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM pcf_component WHERE fund_code=?", (code,))}
        c.close()
        ok = miss = skip = 0
        for d in _weekdays(start_d, end_d):
            d8 = d.strftime("%Y%m%d")
            if d8 in have:
                skip += 1
                continue
            try:
                sid, n = f.fetch_and_store(code, d8)
                ok += 1
                print("  ① PCF %s  snapshot_id=%s  成分=%s" % (d8, sid, n))
            except PCFUnavailable as e:
                miss += 1
                print("  ① PCF %s  无（%s）" % (d8, str(e)[:62]))
            except Exception as e:
                miss += 1
                print("  ① PCF %s  ERR %s: %s" % (d8, type(e).__name__, str(e)[:80]))
        print("① PCF：新增 %d / 已在库 %d / 取不到 %d" % (ok, skip, miss))

    # ② 汇率 ----------------------------------------------------------------
    if args.only in (None, "fx"):
        fx = FXProvider(code)
        r = fx.fetch_chinamoney()
        print("② 中间价最新：%s" % (r,))
        st = fx.fetch_settle_history(start_d.strftime("%Y%m%d"),
                                     end_d.strftime("%Y%m%d"), market=market)
        print("② 结算汇率(参考汇率中值反推, _%s)：%s" % (market, st))

    # ③ 行情 ----------------------------------------------------------------
    if args.only in (None, "quote"):
        codes = _hk_codes(code)
        HK = HKQuoteFetcher()
        ps = (start_d - datetime.timedelta(days=15)).isoformat()
        n = HK.prefetch(codes, ps, e_iso)
        print("③ 港股行情预热：%d 只（%s ~ %s）" % (n, ps, e_iso))
        ne = ETFQuoteFetcher().backfill_price(code, days=600, start=s_iso, end=e_iso)
        print("③ ETF 市价 → fund_nav.price：%d 行" % ne)

    # ④ 校准 ----------------------------------------------------------------
    if args.only in (None, "calib"):
        if HK is None:                       # --only calib 单独跑时自建并预热
            codes = _hk_codes(code)
            HK = HKQuoteFetcher()
            ps = (start_d - datetime.timedelta(days=15)).isoformat()
            n = HK.prefetch(codes, ps, e_iso)
            print("  （预热 %d 只港股行情）" % n)

        v = IOPVVerifier(code)
        v.calc.hk = HK                       # 🔴 共享缓存，否则预热白做
        res = v.verify_all_pending(force=args.force)
        nerr = sum(1 for r in res if r.get("error_bps") is not None)
        nsk = sum(1 for r in res if "skipped" in r)
        print("④ 篮子校准：算出 %d / 跳过 %d" % (nerr, nsk))
        for r in res:
            if "skipped" in r:
                print("     skip  %s  %s" % (r.get("basket_td"), str(r["skipped"])[:80]))
            else:
                tag = " WARN(停牌券)" if r.get("suspended") else ""
                print("     %s  现算=%.2f 官方=%.2f  err=%+.4f bp%s"
                      % (r.get("basket_td"), r.get("computed_basket") or 0,
                         r.get("official_basket") or 0, r.get("error_bps") or 0, tag))

        cc = IOPVCloseCalc(code)
        cc.calc.hk = HK
        res2 = cc.calc_all(force=args.force)
        ok2 = sum(1 for r in res2 if r.get("iopv") is not None)
        sk2 = sum(1 for r in res2 if "skipped" in r)
        print("④ 收盘 IOPV：算出 %d / 跳过 %d" % (ok2, sk2))
        for r in res2:
            if "skipped" in r:
                print("     skip  %s  %s" % (r.get("announce_td"), str(r["skipped"])[:80]))

    # 汇总：历史页会出现的行 ------------------------------------------------
    c = sqlite3.connect(DB_PATH)
    nrow = c.execute(
        "SELECT COUNT(*) FROM (SELECT trade_date FROM pcf_snapshot WHERE fund_code=? "
        "UNION SELECT pre_trade_date FROM pcf_snapshot WHERE fund_code=? "
        "AND pre_trade_date IS NOT NULL)", (code, code)).fetchone()[0]
    nv = c.execute("SELECT COUNT(*) FROM iopv_verify WHERE fund_code=?", (code,)).fetchone()[0]
    nc = c.execute("SELECT COUNT(*) FROM iopv_close WHERE fund_code=?", (code,)).fetchone()[0]
    np = c.execute("SELECT COUNT(*) FROM unified_fund_history WHERE fund_code=? AND price IS NOT NULL",
                   (code,)).fetchone()[0]
    c.close()
    print(LINE)
    print("汇总 %s：历史页行数=%d  iopv_verify=%d  iopv_close=%d  unified_fund_history.price=%d"
          % (code, nrow, nv, nc, np))
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
