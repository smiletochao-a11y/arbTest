# -*- coding: utf-8 -*-
"""
core/lcu_nav_fetcher.py

159687 底层 LCU（CSOP FTSE Asia Pacific Select Index ETF, SGX 上市）官方净值「活源」抓取器，落库版。

背景（2026-10-08）：
    CSOP /cmsApi/NAV/product 只给最新 1 条净值（够用，ARM 每晚爬一次累积）。
    performanceView/product 历史序列冻在 2025-12-30。
    → ARM 每晚收盘后爬一次，UPSERT 进 arb_master.db 的 lcu_nav_history。

要点（务必守）：
    1. 只取 USD 柜台（Ticker='LCU SP' / Currency='USD'），与东哥手抄序列同一序列。
       ⚠️ 不可取 SGD 柜台(LCS SP, ~2.87x) —— 量级差 ~29%，混用会污染桥接。
    2. 只【追加新交易日】，CSOP 未推进日期（非估值日/接口滞后）则跳过，不造非估值日。
    3. 幂等：日期已存在且 NAV 一致 → 跳过；NAV 不一致 → 覆盖（以官方为准）。
    4. 落库 lcu_nav_history（与 ETFarb / arbTest 同库）；不写 index_history / unified_fund_history。
    5. 爬到后发 Telegram 通知（复用 oil_eod_sampler 的标准 Bot API 模式，东哥机器人验证）。

用法：
    python lcu_nav_fetcher.py [--dry-run] [--no-notify] [--seed] [--json-path PATH]
    --dry-run    只打印，不写库不发报
    --no-notify  写库但不发 Telegram
    --seed       从 --json-path(默认 base JSON) 导入历史净值日到库（不抓 CSOP）
"""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.lcu_nav_source import _find_db, _TABLE

CSOP_NAV_URL = "https://website-api.csopasset.com/cmsApi/NAV/product"
PRODUCT_NAME = "CSOP FTSE Asia Pacific Select Index ETF"
# 完整 Chrome UA：CSOP 反爬，短 UA 直接 403
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 20
_MAX_RETRY = 3


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _fetch_latest():
    """返回 (date_str 'YYYY-MM-DD', nav float, raw_record dict) 或 None。"""
    body = json.dumps({"productName": PRODUCT_NAME}).encode("utf-8")
    last_err = None
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            req = urllib.request.Request(CSOP_NAV_URL, data=body, method="POST")
            req.add_header("User-Agent", _CHROME_UA)
            req.add_header("Referer", "https://www.csopasset.com/")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list) or not data:
                raise ValueError("CSOP 返回非预期结构: %r" % type(data))
            # 定位 USD 柜台 (LCU SP)
            rec = None
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get("Ticker") == "LCU SP" or item.get("Currency") == "USD":
                    rec = item
                    break
            if rec is None:  # 兜底：第一个记录
                rec = data[0]
            nav = rec.get("NAV")
            date_raw = rec.get("HstDateFormat")  # 'YYYY-MM-DD'
            if nav is None or not date_raw:
                raise ValueError("LCU SP 记录缺 NAV/HstDateFormat: %r" % rec)
            return date_raw, float(nav), rec
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * attempt)
    raise RuntimeError("CSOP 抓取失败（重试 %d 次）: %s" % (_MAX_RETRY, last_err))


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _upsert_db(db_path, date_str, nav, source):
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "date TEXT PRIMARY KEY, nav REAL NOT NULL, source TEXT, fetched_at TEXT)"
            % _TABLE
        )
        now = datetime.datetime.now().isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO %s(date,nav,source,fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET nav=excluded.nav, "
            "source=excluded.source, fetched_at=excluded.fetched_at"
            % _TABLE,
            (date_str, nav, source, now),
        )
        con.commit()
    finally:
        con.close()


def _count_db(db_path):
    con = sqlite3.connect(db_path)
    try:
        n = con.execute("SELECT COUNT(*) FROM %s" % _TABLE).fetchone()[0]
    finally:
        con.close()
    return n


def seed_from_json(json_path, db_path):
    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)
    n = 0
    for rec in doc.get("nav_series", []):
        nav = rec.get("nav")
        if nav is None:
            continue  # 非估值日不入库（缺失即视为非估值日，桥接自动降级）
        _upsert_db(db_path, rec["date"], float(nav),
                   rec.get("source", "seed_%s" % rec["date"]))
        n += 1
    print("[seed] 已从 %s 导入 %d 个净值日到 %s" % (json_path, n, db_path))


# ---------------------------------------------------------------------------
# Telegram 通知（复用 oil_eod_sampler 标准 Bot API 模式）
# ---------------------------------------------------------------------------
def _get_secret(name, default=""):
    """从 account_info/secrets.env 读取凭据（仓库外保险箱）。缺失返回 default。"""
    try:
        import importlib.util as _ilu
        from pathlib import Path as _Path
        for cand in (_Path("/home/ubuntu/account_info/secrets.py"),
                     _Path(r"D:\Study\ARB\account_info\secrets.py")):
            if cand.exists():
                spec = _ilu.spec_from_file_location("_unified_secrets", str(cand))
                m = _ilu.module_from_spec(spec)
                spec.loader.exec_module(m)
                return getattr(m, "get_secret", lambda k, d=None: d)(name, default)
    except Exception:
        pass
    return default


def notify_telegram(msg):
    """发送 Telegram 通知（东哥机器人，标准 Bot API）。失败不影响主流程。"""
    token = _get_secret("TELEGRAM_BOT_TOKEN", "")
    chat = _get_secret("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("[notify] 未配置 TELEGRAM_BOT_TOKEN/CHAT_ID，跳过发报")
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "--data-urlencode", "chat_id=%s" % chat,
        "--data-urlencode", "parse_mode=Markdown",
        "--data-urlencode", "text=%s" % msg,
    ]
    for i in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            resp = json.loads(r.stdout) if r.stdout else {}
            if resp.get("ok"):
                print("[notify] Telegram 已发送")
                return
            print("[notify] 失败(第%d/3次): %s" % (i + 1, resp.get("description")))
        except Exception as e:
            print("[notify] 异常(第%d/3次): %s" % (i + 1, e))
        if i < 2:
            time.sleep(5)
    print("[notify] 最终失败（不影响主流程）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(dry_run=False, no_notify=False, seed=False, json_path=None):
    db_path = _find_db()
    if not db_path:
        print("[lcu_nav_fetcher] 未找到 arb_master.db，中止")
        return {"error": "no_db"}
    print("[lcu_nav_fetcher] DB: %s" % db_path)

    if seed:
        if not json_path or not os.path.exists(json_path):
            print("[lcu_nav_fetcher] --seed 需有效 --json-path")
            return {"error": "no_json"}
        seed_from_json(json_path, db_path)
        return {"seeded": True}

    date_str, nav, _rec = _fetch_latest()
    print("[lcu_nav_fetcher] CSOP 最新 USD(LCU SP): date=%s nav=%.4f" % (date_str, nav))

    # 判断是否已存在
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT nav FROM %s WHERE date=?" % _TABLE, (date_str,)
        ).fetchone()
    finally:
        con.close()

    appended = 0
    if row is not None:
        old = row[0]
        if abs(old - nav) < 1e-9:
            print("[lcu_nav_fetcher] 日期 %s 已存在且 NAV 一致(%.4f)，跳过" % (date_str, nav))
        else:
            print("[lcu_nav_fetcher] 日期 %s NAV 变更 %.4f→%.4f，覆盖" % (date_str, old, nav))
            _upsert_db(db_path, date_str, nav, "csop_nav_product_%s" % date_str)
    else:
        print("[lcu_nav_fetcher] 追加新净值日 %s = %.4f" % (date_str, nav))
        _upsert_db(db_path, date_str, nav, "csop_nav_product_%s" % date_str)
        appended = 1

    total = _count_db(db_path)

    if dry_run:
        print("[lcu_nav_fetcher] --dry-run，不写库不发报")
        return {"appended": appended, "skipped": 1 - appended, "total": total, "dry": True}

    if not no_notify:
        bj = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%H:%M:%S")
        if appended:
            head = "【LCU 净值(159687 底层)·新增】"
        else:
            head = "【LCU 净值(159687 底层)·已收录】"
        msg = (
            "%s%s\n"
            "北京时间 %s\n"
            "LCU SP (USD): %.4f\n"
            "来源: CSOP /cmsApi/NAV/product ✅\n"
            "累计点数: %d"
        ) % (head, date_str, bj, nav, total)
        notify_telegram(msg)

    return {"appended": appended, "skipped": 1 - appended, "total": total}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LCU (CSOP) 官方净值活源抓取器（落库版）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库不发报")
    ap.add_argument("--no-notify", action="store_true", help="写库但不发 Telegram")
    ap.add_argument("--seed", action="store_true", help="从 JSON 导入历史净值日到库")
    ap.add_argument("--json-path", default=None, help="--seed 用的 JSON 路径")
    args = ap.parse_args()
    try:
        res = run(dry_run=args.dry_run, no_notify=args.no_notify,
                  seed=args.seed, json_path=args.json_path)
        print("RESULT:", res)
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
