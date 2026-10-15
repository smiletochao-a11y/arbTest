#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/backfill_pcf_cash_value.py — 回填 `pcf_component.cash_value`
====================================================================================
背景（docs/40 §七·15 / docs/21 §二·E）
------------------------------------------------------------------------------------
深交所 PCF 宽表里「**必须现金替代**」的成分，`股票数量` 列填 0，其**货值**写在
『申购替代金额』/『赎回替代金额』两列（例：159960 网易 = 25,444.3600）。

旧解析层（`iopv_pcf.PCFFetcher.parse_szse`）按「深市不给每股替代金额」的假设，
一律把这两列丢成 NULL ⇒ 引擎按 `shares>0` 取成分 ⇒ **整只成分漏出篮子** ⇒
自算 IOPV 偏低 ⇒ 前端出现巨额假溢价：

| 标的 | 漏计货值占比 | 修复前自算偏离 |
|---|---|---|
| 159960 恒生中国企业ETF平安 | 7.60% | **−767.5bp** |
| 159143 港股通央企红利ETF平安 | 3.04% | −286.5bp |
| 159271 恒生ETF鹏华 | 2.48% | −245.5bp |
| 159131 港股通信息技术ETF华宝 | 1.11% | −103.6bp |

修复（2026-09-17，东哥定 ① 治本）：解析层落 `cash_value` 列，引擎按
「D 货值 ÷ (D 日未调整收盘 × 汇率) 反推等效股数」并入实时篮子。

本脚本做的是**存量数据回填**：**不重新抓取**，直接从已落库的 `raw_text`
重解析（深交所 CDN 宽表可回溯上市首日，raw_text 一直留着）⇒ 零网络、可重跑。

用法
------------------------------------------------------------------------------------
    python core/backfill_pcf_cash_value.py            # 回填
    python core/backfill_pcf_cash_value.py --dry-run  # 只统计不写库
    python core/backfill_pcf_cash_value.py --code 159960
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "etfarb.db")  # 占位，下方 import db 后重赋
sys.path.insert(0, HERE)
import db as _db   # noqa: E402  core/db.py —— 单库方案 arb_master.db
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

from pcf_fetcher import parse_pcf_txt   # noqa: E402
from iopv_pcf import init_db            # noqa: E402


def _is_szse_wide(text):
    """深交所宽表：带「YYYY-MM-DD日 信息内容」日期段 + 「证券代码」列头。"""
    return ("信息内容" in text) and ("证券代码" in text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--code", default=None, help="只处理某只标的")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db(args.db)        # 幂等补列（pcf_component.cash_value）
    c = sqlite3.connect(args.db)
    sql = ("SELECT id, fund_code, trade_date, pre_trade_date, raw_text FROM pcf_snapshot "
           "WHERE raw_text IS NOT NULL AND raw_text<>''")
    params = ()
    if args.code:
        sql += " AND fund_code=?"
        params = (args.code,)
    snaps = c.execute(sql, params).fetchall()

    n_snap = n_updated = n_hit = 0
    tot = 0.0
    per_fund = {}
    for sid, code, td, pre_td, raw in snaps:
        if not _is_szse_wide(raw):
            continue                        # 沪市 XML / 易方达 JSON 无此类行
        try:
            info = parse_pcf_txt(raw, code)
        except Exception as e:
            print("  ⚠ %s %s 解析失败：%s" % (code, td, str(e)[:80]))
            continue
        n_snap += 1
        sub = 0.0
        for x in info.get("components", []):
            if x.get("raw_code") == "159900":
                continue
            vol = x.get("volume") or 0
            amt = x.get("fixed_amount")
            if vol > 0 or not amt or float(amt) <= 0:
                continue
            sub += float(amt)
            n_hit += 1
            sql2 = ("UPDATE pcf_component SET cash_value=? "
                    "WHERE snapshot_id=? AND stock_code=?")
            if args.dry_run:
                old = c.execute("SELECT cash_value FROM pcf_component WHERE snapshot_id=? AND stock_code=?",
                                (sid, x["raw_code"])).fetchone()
                assert old is not None, "快照 %s 无成分行 %s（库/文本不一致）" % (sid, x["raw_code"])
            else:
                cur = c.execute(sql2, (float(amt), sid, x["raw_code"]))
                n_updated += cur.rowcount
        if sub:
            tot += sub
            per_fund[code] = per_fund.get(code, 0.0) + sub
    if not args.dry_run:
        c.commit()

    print("扫描深市宽表快照 %d 份；命中「数量=0 且货值>0」成分 %d 行" % (n_snap, n_hit))
    if per_fund:
        print("按标的汇总（一个篮子口径的货值合计，跨日累加）：")
        for k in sorted(per_fund, key=lambda x: -per_fund[x]):
            print("   %-8s %14.2f CNY" % (k, per_fund[k]))
    print("货值总计 %.2f CNY；写库行数 %d%s"
          % (tot, n_updated, "（dry-run 未写）" if args.dry_run else ""))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
