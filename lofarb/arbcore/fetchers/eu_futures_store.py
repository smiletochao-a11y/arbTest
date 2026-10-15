# -*- coding: utf-8 -*-
"""
[AI-2026-10-08] 欧系指数期货 —— 锚点持久化 + 估值单组件装配（架构层）。

三层分工（源可替换、层间解耦）：
  eu_futures.py          纯取数 + 解析（不落库、不管调度）
  eu_futures_store.py    本文件：落库/读库 + 把「锚点 → 当前」折成 basket_valuation 单组件
  eu_futures_sampler.py  调度：每欧系交易日在当地 17:30 冻结锚点

为什么锚点必须落库（而不是每次现算）
--------------------------------------
锚点是「当地现货收盘 17:30」那一刻的**一次性**取数。而 2026-10-09 起 investing.com 已
被 Cloudflare **按 IP 封禁**（连首页都 403，换 UA/加头/curl 全无效；封禁依据见
`eu_futures.py` 模块 docstring）⇒ 东哥拍板彻底删净该源 ⇒ **本模块不再有任何历史 1m/日线可回捞**。
现存两个源都只有"当前/最新"（onvista 实时报价、boerse-frankfurt 最新结算），**都给不了历史锚点**。
⇒ 锚点**只能"当场冻结"存库**；估值侧只读库，从而与"谁来采 / 采在本机还是 ARM"彻底解耦。

表 eu_futures_quotes（幂等建表）
  trade_date  当地(巴黎/柏林)交易日 YYYY-MM-DD
  symbol      FDAX / FCE
  kind        'anchor'（17:30 冻结）；预留 'rt'（滚动实时留痕）
  price       期货价
  ref_price   参考价（锚点行 = 源侧 previousLast；rt 行 = 该时点锚点）
  src         onvista / boerse-frankfurt（**investing 已移除**）
  quality     源侧延迟标注（DLY / SETTLE / RT），缺失为 None
  quote_ts    报价自身时间戳（naive UTC，ISO）
  fetched_at  采集时刻（UTC，ISO）
  UNIQUE(trade_date, symbol) WHERE kind='anchor'  ⇒ 同一天同品种锚点幂等覆盖。

口径红线
--------
  * 锚点只认"当日库里有行"；缺就是缺，**不做跨日期兜底**（宁缺勿假）。
  * 取当前价失败 → t_day_component 返回 None，绝不拿锚点自比（那会恒等于 0 涨幅）。
  * 汇率（eur_cny_mid / eur_cny_spot）不在此层处理，由调用方（fund_service）注入。
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import eu_futures

logger = logging.getLogger(__name__)

# 锚点保留天数（含今天）。单日 2 品种 ≈ 2 行 ⇒ 45 天约 90 行，体积可忽略。
RETAIN_DAYS = 45

# ---- 陈旧基准日的负缓存（AI 2026-10-09，见 resolve_anchor docstring）----
#   (symbol, trade_date) 四级回捞失败 ⇒ 记入本集合，之后直接返回 None，不再重复打注定
#   失败的网络请求、不再打日志。由 save_anchor() 主动解除（采样器冻结成功即自愈）。
_NEG = set()
_NEG_MAX = 256          # 上限保护（陈旧基准日长期不动时不会无限增长）

# ---- 日志去重（AI 2026-10-09）----
#   估值层每轮询都会调 t_day_component（前端 30s 轮询 ⇒ 每只基金每 30s 一次）。
#   「缺数据」是常态结果而非事件 ⇒ 同一 key 只在首次按给定级别打印，之后静默。
_LOG_ONCE_SEEN = set()
_LOG_ONCE_MAX = 512


def _log_once(key, lvl, msg, *args):
    """同一个 key 只在**首次**按 lvl 打；之后的重复静默降debug（常态结果，不是事件）。"""
    if key in _LOG_ONCE_SEEN:
        return
    if len(_LOG_ONCE_SEEN) >= _LOG_ONCE_MAX:
        _LOG_ONCE_SEEN.clear()
    _LOG_ONCE_SEEN.add(key)
    lvl(msg, *args)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS eu_futures_quotes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'anchor',
    price       REAL NOT NULL,
    ref_price   REAL,
    src         TEXT,
    quality     TEXT,
    quote_ts    TEXT,
    fetched_at  TEXT NOT NULL
)
"""
CREATE_UQ_ANCHOR = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_eu_futures_anchor "
    "ON eu_futures_quotes(trade_date, symbol) WHERE kind='anchor'"
)
CREATE_IDX = (
    "CREATE INDEX IF NOT EXISTS ix_eu_futures_sym_date "
    "ON eu_futures_quotes(symbol, trade_date)"
)


# ---------------------------------------------------------------------------
# 库路径
# ---------------------------------------------------------------------------
def default_db_path() -> str:
    """单库真库：优先走 arbcore 统一解析；不可导入时回退 ARM 路径。"""
    try:
        from arbcore.database.db_manager import _resolve_db_path
        return _resolve_db_path()
    except Exception:
        return "/home/ubuntu/arbtest/database/arb_master.db"


def _connect(db_path=None):
    return sqlite3.connect(db_path or default_db_path(), timeout=15)


def ensure_table(db_path=None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(CREATE_SQL)
        conn.execute(CREATE_UQ_ANCHOR)
        conn.execute(CREATE_IDX)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def save_anchor(db_path, symbol, trade_date, price, ref_price=None,
                src=None, quality=None, quote_ts=None, retain_days=RETAIN_DAYS) -> bool:
    """冻结一条锚点（幂等覆盖）。trade_date: 'YYYY-MM-DD'（当地日）。price: float。"""
    if not symbol or not trade_date or price is None:
        return False
    try:
        price = float(price)
    except Exception:
        return False
    if price <= 0:
        return False
    if isinstance(quote_ts, datetime):
        quote_ts = quote_ts.strftime("%Y-%m-%dT%H:%M:%S")
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect(db_path)
    try:
        conn.execute(CREATE_SQL)
        conn.execute(CREATE_UQ_ANCHOR)
        conn.execute(
            "INSERT INTO eu_futures_quotes "
            "(trade_date, symbol, kind, price, ref_price, src, quality, quote_ts, fetched_at) "
            "VALUES (?, ?, 'anchor', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(trade_date, symbol) WHERE kind='anchor' DO UPDATE SET "
            "price=excluded.price, ref_price=excluded.ref_price, src=excluded.src, "
            "quality=excluded.quality, quote_ts=excluded.quote_ts, fetched_at=excluded.fetched_at",
            (trade_date, symbol, price, ref_price, src, quality, quote_ts, fetched_at),
        )
        # 滚动保留：删掉早于 cutoff 的锚点（不动 rt 行）
        try:
            cutoff = (datetime.strptime(trade_date, "%Y-%m-%d").date()
                      - timedelta(days=retain_days - 1)).strftime("%Y-%m-%d")
            conn.execute(
                "DELETE FROM eu_futures_quotes WHERE kind='anchor' AND trade_date < ?",
                (cutoff,),
            )
        except Exception:
            pass
        conn.commit()
        # 采样器冻结成功 ⇒ 解除该 (symbol, date) 的负缓存/去重日志键，下次估值即可正常取锚点
        _NEG.discard((symbol, str(trade_date)))
        _LOG_ONCE_SEEN.discard("missing_anchor:%s/%s" % (symbol, trade_date))
        return True
    except Exception as e:
        logger.warning("eu_futures_store 写锚点失败 %s %s: %s", symbol, trade_date, e)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_anchor(db_path, symbol, trade_date):
    """读指定当地日锚点。返回 dict(price/ref_price/src/quality/quote_ts/fetched_at) 或 None。"""
    if not symbol or not trade_date:
        return None
    conn = _connect(db_path)
    try:
        conn.execute(CREATE_SQL)
        row = conn.execute(
            "SELECT price, ref_price, src, quality, quote_ts, fetched_at "
            "FROM eu_futures_quotes WHERE symbol=? AND trade_date=? AND kind='anchor'",
            (symbol, trade_date),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    return {"price": float(row[0]), "ref_price": row[1], "src": row[2],
            "quality": row[3], "quote_ts": row[4], "fetched_at": row[5],
            "from": "db"}


def load_recent_anchors(db_path, symbol, limit=10):
    """最近若干条锚点（诊断用，倒序）。"""
    conn = _connect(db_path)
    try:
        conn.execute(CREATE_SQL)
        rows = conn.execute(
            "SELECT trade_date, price, src, quality, quote_ts FROM eu_futures_quotes "
            "WHERE symbol=? AND kind='anchor' ORDER BY trade_date DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [{"trade_date": r[0], "price": r[1], "src": r[2], "quality": r[3], "quote_ts": r[4]}
            for r in rows]


def resolve_anchor(db_path, symbol, trade_date, tolerance_min=20):
    """锚点解析（**两级**，绝不跨日期兜底）：

    ① 库里当日锚点（采样器冻结的 17:30 真实 tick）——**唯一可靠来源**
    ② boerse-frankfurt 最新结算（仅 FDAX，且**日期必须吻合**）——抗限流兜底
    全缺 → None（宁缺勿假）。

    🔴 2026-10-09 东哥拍板删净 investing.com（Cloudflare 按 IP 封禁本机，实测首页也 403，
       换 UA/加头/curl 全无效；详见 eu_futures.py 模块 docstring 的封禁依据表）。
       ⇒ 原 ②investing 1m 回捞、③investing 日线收盘 两级**已永久移除**，本模块不再提供任何历史 K 线。
    ④ 的口径依据仍留档：Eurex FDAX 日收盘≈当日结算价≈当地 17:30
       （10-08 实测 investing 8826 日线 10-07 收 25226 == boerse-frankfurt 结算价 25226）——
       故「bf 当日结算价」可当 17:30 锚点的代理；但 bf **只给最新日、无历史**，
       基准日 ≠ 最新日时依旧拿不到 ⇒ **德国线必须靠采样器每日冻结**。
    法国 FCE：bf 不覆盖（只有 Xetra/DAX）、onvista 无该合约 ⇒ **恒 None，显「—」，绝不兜底**。

    🔴 陈旧基准日**记忆化**（AI 2026-10-09）：QDII 欧美的 `base_date` = 最近已公布净值日，
       长假 / 基金公司延迟时会停在数周前（如 159561 停在 09-30）。此时本函数**注定失败**
       ⇒ ① 反复打注定失败的 bf 请求 ② 日志刷屏。
       故：同一 (symbol, trade_date) 失败过一次就**记入 `_NEG` 并直接返回 None**，
       锚点行一旦由采样器落库（`save_anchor`）即自动解除记忆，重新走两级链。
    """
    a = load_anchor(db_path, symbol, trade_date)
    if a:
        return a
    if (symbol, str(trade_date)) in _NEG:
        return None
    try:
        d = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
    except Exception:
        return None
    bp, bd = eu_futures.bf_latest_settlement(symbol)
    if bp and bd == d.strftime("%Y-%m-%d"):
        return {"price": float(bp), "ref_price": None, "src": "boerse-frankfurt",
                "quality": "SETTLE", "quote_ts": bd + "T00:00:00",
                "fetched_at": None, "from": "bf"}
    # 全缺 ⇒ 记住，避免每轮询重跑一遍注定失败的请求（AI 2026-10-09）
    if len(_NEG) >= _NEG_MAX:
        _NEG.clear()
    _NEG.add((symbol, str(trade_date)))
    return None


# ---------------------------------------------------------------------------
# 估值装配：折成 basket_valuation 的单组件
# ---------------------------------------------------------------------------
def t_day_component(db_path, fund_code, base_date, tolerance_min=20):
    """
    给指定基金（513080/513030/159561）装配实时估值的单组件 dict：

        {'symbol','base_price','current_price','weight'=1.0, 'anchor_ts','src','delay_min','anchor_from'}

    base_price    = 基准日(base_date) 现货收盘 17:30 的期货价（锚点）
    current_price = 当前期货价（最新可用）

    返回 None 表示缺数据（锚点缺失 或 当前价缺失）——调用方应保持 rt_val=None（前端显 "-"），
    绝不退化成拿锚点自比（那会恒等于 0 涨幅、把"缺数据"伪装成"没动"）。
    """
    symbol = eu_futures.FUND_TO_FUTURE.get(str(fund_code))
    if not symbol:
        return None
    anchor = resolve_anchor(db_path, symbol, base_date, tolerance_min=tolerance_min)
    if not anchor or not anchor.get("price"):
        # 缺锚点是**预期常态**（QDII 欧美 base_date 停在最近已公布净值日，长假/基金公司
        # 延迟时会陈旧数周；历史 1m 窗口外 + 当日结算源只给"最新" ⇒ 四级全落空）。
        #   [AI-2026-10-09 东哥拍板] 前端已显「-」，属正常缺数据，**不是故障**
        #   ⇒ 降到 logger.debug（原先 info 仍会在每次进程重启首刷屏；WARNING 更吵，更错）。
        #   排障时开 debug，或直接看库 eu_futures_quotes 是否有当日 trade_date。
        _log_once("missing_anchor:%s/%s" % (symbol, base_date), logger.debug,
                  "[%s] 欧系期货锚点缺失 symbol=%s base_date=%s（rt 保持 None，"
                  "预期常态：需采样器当日冻结，历史回捞不到）", fund_code, symbol, base_date)
        return None
    q = eu_futures.current_quote(symbol)
    if not q or not q.get("price"):
        _log_once("missing_quote:%s" % symbol, logger.debug,
                  "[%s] 欧系期货当前价缺失 symbol=%s（rt 保持 None）", fund_code, symbol)
        return None
    delay = None
    if q.get("ts"):
        delay = round((datetime.utcnow() - q["ts"]).total_seconds() / 60.0, 1)
    return {
        "symbol": symbol,
        "base_price": float(anchor["price"]),
        "current_price": float(q["price"]),
        "weight": 1.0,
        "anchor_ts": anchor.get("quote_ts"),
        "anchor_from": anchor.get("from"),
        "src": q.get("source"),
        "delay_min": delay,
    }


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------
def probe(db_path=None, fund_codes=None):
    """打印锚点表 + 每个在册基金的"锚点→当前"折算（一键验证架构是否通）。"""
    db_path = db_path or default_db_path()
    print(f"DB: {db_path}")
    print(f"锚点保留: {RETAIN_DAYS} 天")
    for sym in eu_futures.INSTRUMENTS:
        rows = load_recent_anchors(db_path, sym, limit=8)
        print(f"\n[{sym}] {eu_futures.INSTRUMENTS[sym]['name']}  锚点行数(近8)={len(rows)}")
        for r in rows:
            print(f"    {r['trade_date']}  price={r['price']}  src={r['src']}  q={r['quality']}  ts={r['quote_ts']}")
    print("\n---- 单组件折算 ----")
    codes = fund_codes or list(eu_futures.FUND_TO_FUTURE.keys())
    today = datetime.utcnow().date()
    for code in codes:
        sym = eu_futures.FUND_TO_FUTURE.get(str(code))
        # 取库里该品种最新锚点日期作为 base_date 演示
        rows = load_recent_anchors(db_path, sym, limit=1) if sym else []
        base_date = rows[0]["trade_date"] if rows else today.strftime("%Y-%m-%d")
        comp = t_day_component(db_path, code, base_date)
        print(f"  {code} → {sym}  base_date={base_date}  component={comp}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    probe()
