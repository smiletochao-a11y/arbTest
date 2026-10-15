#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
欧系指数期货「锚点」采样器（法国 CAC40-FCE / 德国 DAX-FDAX）。

作用
----
每欧系交易日在当地(巴黎/柏林) 17:30 —— 现货收盘那一刻 —— 冻结对应期货价，落库
`eu_futures_quotes(kind='anchor')`。这是 T 日实时估值「锚点 → 当前」的基准端。
锚点**必须当场冻结**：2026-10-09 起 investing.com 已被 Cloudflare **按 IP 封禁**
（连首页都 403，换 UA/加头/curl 全无效），东哥拍板**彻底删净该源** ⇒ 事后**没有任何历史
1m/日线可回捞**（现存 onvista / boerse-frankfurt 都只给"当前/最新"）。
⇒ 本采样器是锚点的**唯一来源**，必须跑起来（ARM 7×24，见 §10.5）。
🔴 法国 FCE：onvista 无该合约、boerse-frankfurt 只覆盖 Xetra(DAX) ⇒ **FCE 无源可采，
   513080 恒显「—」**（宁缺勿假，绝不兜底）。

为什么用「多触发 + 就近者胜」
------------------------------
免费源是延迟源（onvista 实测 ~15min）：墙钟 17:30 那一刻抓到的 `datetimeLast`
可能只到 ~17:15。故本脚本设计为「窗口内反复触发」，每条新报价与目标时刻(当地 17:30)
比距离，**更近者覆盖**已存锚点 ⇒ 随延迟馈送推进，锚点自动逼近真实 17:30 的成交。
（东哥 10-08 已接受 ~15min 延迟源；FCE 锚点取不到精确 tick 亦接受。）

调度
----
- 本机：手动跑 `--point anchor`。
- ARM：systemd timer 每 5 分钟触发，脚本自判当地 17:30–18:30 窗口内才采（见 deploy/systemd/）。

用法
----
    python eu_futures_sampler.py --point anchor          # 强制冻结今天锚点
    python eu_futures_sampler.py --point anchor --dry-run
    python eu_futures_sampler.py --probe                 # 锚点表 + 单组件折算
    python eu_futures_sampler.py --diagnose              # 各源密度/延迟
    python eu_futures_sampler.py --db /path/arb_master.db
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# ---------- sys.path：定位 src 根（本文件在 src/ArbDashboard/backend/holding_valuation/） ----------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from arbcore.fetchers import eu_futures            # noqa: E402
from arbcore.fetchers import eu_futures_store as store  # noqa: E402

# ---------- 配置 ----------
DEFAULT_DB = store.default_db_path()
LOG_DIR = "/home/ubuntu/arbtest/logs"
LOG_FILE = os.path.join(LOG_DIR, "eu_futures_sampler.log")

# 锚点目标时刻：当地现货收盘（巴黎/柏林）
ANCHOR_HHMM = (17, 30)
# 自动模式的采集窗口（当地分钟数区间）：17:30 起、至 18:30 止
WINDOW_START_MIN = 17 * 60 + 30
WINDOW_END_MIN = 18 * 60 + 30

# 欧系(巴黎/柏林)休市日 —— 近似表，仅作采样守卫（判错只影响是否漏采一天，无副作用）。
# ⚠️ 每年需按 Euronext / Xetra 官方日历复核；半日市(12-24 / 12-31)按休市处理。
EU_HOLIDAYS_2026 = {
    "2026-01-01",  # 元旦
    "2026-04-03",  # Good Friday
    "2026-04-06",  # Easter Monday
    "2026-05-01",  # 劳动节
    "2026-12-24",  # 平安夜（半日）
    "2026-12-25",  # 圣诞
    "2026-12-26",  # 节礼日
    "2026-12-31",  # 跨年（半日）
}

# 采样成功 → Telegram 通知（复用 ARM 上 Hermes；默认关，加 --notify 才发）
HERMES_BIN = "/home/ubuntu/.local/bin/hermes"
NOTIFY_TARGET = os.environ.get("HERMES_NOTIFY_TARGET", "")


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def eu_now() -> datetime:
    """当前巴黎/柏林本地时间（naive）。复用 eu_futures 内置 EU DST 规则，不依赖 tzdata。"""
    return datetime.utcnow() + timedelta(hours=eu_futures.eu_utc_offset_hours(datetime.utcnow()))


def is_eu_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in EU_HOLIDAYS_2026


def in_window(now: datetime) -> bool:
    m = now.hour * 60 + now.minute
    return WINDOW_START_MIN <= m <= WINDOW_END_MIN


def _minute_dist(ts_naive_utc, target_utc) -> float:
    """|报价市场时刻 − 目标时刻|（分钟）。ts 为空 → 无穷大（永不覆盖已有锚点）。"""
    if ts_naive_utc is None:
        return float("inf")
    return abs((ts_naive_utc - target_utc).total_seconds()) / 60.0


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def capture_anchor(db_path, trade_date, force=False, dry_run=False, log=None) -> int:
    """冻结 trade_date 当日各品种锚点。返回写入条数。"""
    log = log or logging.getLogger("eu_futures")
    try:
        _d = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
    except Exception:
        _d = datetime.utcnow().date()
    target_utc = eu_futures.anchor_utc(_d, "17:30")
    written = 0
    for sym, meta in eu_futures.INSTRUMENTS.items():
        try:
            q = eu_futures.current_quote(sym, use_cache=False)
        except Exception as e:
            log.error("取当前价失败 %s: %s", sym, e)
            continue
        if not q or not q.get("price"):
            log.warning("⚠️ %s 无当前报价，跳过", sym)
            continue

        cand_dist = _minute_dist(q.get("ts"), target_utc)
        existing = store.load_anchor(db_path, sym, trade_date)
        if existing and not force:
            try:
                old_ts = datetime.strptime(existing["quote_ts"][:19], "%Y-%m-%dT%H:%M:%S") \
                    if existing.get("quote_ts") else None
            except Exception:
                old_ts = None
            old_dist = _minute_dist(old_ts, target_utc)
            if cand_dist >= old_dist:
                log.info("↺ %s 已有更近锚点(dist=%.1fmin ≤ 新 %.1fmin)，保留不覆盖",
                         sym, old_dist, cand_dist)
                continue

        log.info("📌 %s 锚点 price=%s src=%s q=%s quote_ts=%s dist=%.1fmin (target_utc=%s)",
                 sym, q["price"], q.get("source"), q.get("quality"),
                 q.get("ts"), cand_dist, target_utc)
        if dry_run:
            written += 1
            continue
        ok = store.save_anchor(db_path, sym, trade_date, q["price"],
                               ref_price=q.get("previous"), src=q.get("source"),
                               quality=q.get("quality"), quote_ts=q.get("ts"))
        if ok:
            written += 1
    return written


def notify(trade_date, rows):
    import subprocess
    if not NOTIFY_TARGET:
        print("(未配置 HERMES_NOTIFY_TARGET，跳过通知)")
        return
    bj = (datetime.utcnow() + timedelta(hours=8)).strftime("%H:%M")
    parts = " ".join(f"{k}={v}" for k, v in sorted(rows.items()))
    msg = f"{bj} 欧系期货锚点 {trade_date} {parts} → 落库 ✅"
    try:
        subprocess.run([HERMES_BIN, "send", "--to", NOTIFY_TARGET, msg],
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "HOME": os.environ.get("HOME", "/home/ubuntu")})
    except Exception as e:
        print(f"通知失败(不影响落库): {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="欧系指数期货锚点采样器（当地 17:30 冻结 FCE/FDAX）")
    ap.add_argument("--point", choices=["anchor"], help="强制采今天锚点(测试用)")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标 sqlite 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--force", action="store_true", help="忽略已有锚点，强制覆盖")
    ap.add_argument("--notify", action="store_true", help="落库后发 Telegram")
    ap.add_argument("--probe", action="store_true", help="打印锚点表 + 单组件折算后退出")
    ap.add_argument("--diagnose", action="store_true", help="打印各源密度/延迟后退出")
    args = ap.parse_args()

    # 日志走 stdout，由 systemd unit 重定向到 LOG_FILE（见 deploy/systemd/）；
    # 本机手动跑直接看终端。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("eu_futures")

    if args.diagnose:
        eu_futures.diagnose()
        return 0
    if args.probe:
        store.ensure_table(args.db)
        store.probe(args.db)
        return 0

    now = eu_now()
    trade_date = now.strftime("%Y-%m-%d")
    point = args.point
    if point is None:
        if not in_window(now):
            log.info("当地 %s 不在采集窗口(17:30–18:30)，跳过", now.strftime("%H:%M"))
            return 0
        point = "anchor"
    if not args.point and not is_eu_trading_day(now):
        log.info("%s 非欧系交易日，跳过采样", trade_date)
        return 0

    log.info("采锚点 trade_date=%s(当地) point=%s db=%s", trade_date, point, args.db)
    n = capture_anchor(args.db, trade_date, force=args.force, dry_run=args.dry_run, log=log)
    log.info("完成：写入 %d 条%s", n, "（dry-run 未落库）" if args.dry_run else "")

    if n and args.notify and not args.dry_run:
        rows = {}
        for sym in eu_futures.INSTRUMENTS:
            a = store.load_anchor(args.db, sym, trade_date)
            if a:
                rows[sym] = a["price"]
        notify(trade_date, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
