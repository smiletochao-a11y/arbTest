# -*- coding: utf-8 -*-
"""跨境 ETF 看板 —— 标的池与档案（P1，2026-09-17 立项，docs/40）。

权威来源
--------
`data/etf_universe_jsl_A.tsv`（亚洲 205 只）/ `_E.tsv`（欧美 41 只）
= 东哥提供的**集思录两表原始快照**（2026-09-17 11:43 抓取）。
实测：集思录的「亚洲 / 欧美」分组 = **净值基准日 D 的划分**（亚洲 D=T-1、欧美 D=T-2），
零例外，与 docs/21 红线 9 同源 —— 所以「市场」列可直接照它分组。

🔴 职责边界（不搞双重真相源）
------------------------------
1. 本模块**不算任何 IOPV**：一切数字由 `core/iopv_calc.IOPVCalculator` 产出。
2. 只写两张表：
     · `fund_profile`     —— 展示档案（集思录列 + 分流结果），**不参与计算**
     · `fund_iopv_config` —— 估值配置 **seed**，且 **enabled=0**
       （不进主看板的每日 PCF 检查 / 校准 / 告警扫描，避免 246 只污染现有链路）
3. 分流结果 `quote_path` **UI 不展示**，仅供代码选通道。

分流判据（2026-09-17 定，docs/40 §六）
--------------------------------------
| 条件 | 通道 |
|---|---|
| 成分（排除深市虚拟行 `159900`）**全部 `market='HK'` 且数量 ≥ 5** | `perstock` 逐券 |
| 其余（含境外/含 A 股/**成分数 < 5**） | `proxy` 指数代理 |

· 境外成分（`'OS'`：美/日/韩股…）在 A 股时段**无实时行情源** ⇒ 只能代理。
· **低覆盖（< 5 只）** 是「单券 FoF」的标志：持的是**另一只 ETF 的份额**
  （513130 持 `3033`、520830 持 `2830`），用其市价会引入那只 ETF 自身的折溢价 ⇒ 必须代理。
  ⚠️ 这类标的的 `market` 全是港股，**只看 market 会误判成逐券**。
"""
import csv
import datetime
import os
import sqlite3
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PROJ = os.path.dirname(_HERE)
DATA_DIR = os.path.join(PROJ, "data")
DB_PATH = os.path.join(DATA_DIR, "etfarb.db")  # 占位，下方 import db 后重赋

import db as _db   # noqa: E402  core/db.py —— 导入即 ensure_schema()，保证新表存在
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

# 集思录两表 → 本地快照文件
JSL_FILES = {"A": "etf_universe_jsl_A.tsv", "E": "etf_universe_jsl_E.tsv"}
JSL_MARKET_NAME = {"A": "亚洲", "E": "欧美"}

# 集思录「相关指数」关键词 → 汇率对（仅**指数代理**通道用；逐券通道只用 HKD/CNY）。
# 🔴 未命中**不猜**，留 NULL ⇒ FXProvider 兜底 'HKD/CNY'
#    （对纯港股类恰好正确；代理类 P1 不启用，P3 补 EUR/GBP/KRW 时在此登记）。
_FX_BY_INDEX = [
    ("日经", "JPY/CNY"), ("东证", "JPY/CNY"),
    ("纳斯达克", "USD/CNY"), ("纳指", "USD/CNY"),
    ("标普", "USD/CNY"), ("道琼斯", "USD/CNY"), ("美国50", "USD/CNY"),
]

# 集思录「相关指数」→ `index_proxy.INDEX_CODES` 键（**指数代理**通道用）。
# 🔴 只登记**已实测有源**的；未登记 / 映射为 None 的 ⇒ 该只 P1 不出 IOPV（前端显「—」），
#    但档案与行情列照常展示。P3 逐档补源（找东财 secid 或退化为不展示）。
INDEX_KEY_MAP = {
    "日经225": "N225",
    "纳斯达克100": "NDX",
    "标普500": "SP500",
    "道琼斯工业平均指数": "DJI",
    "德国DAX": "DAX",
    "法国CAC40": "CAC",
    "巴西IBOVESPA": "BVSP",
    "富时沙特阿拉伯": "SAUDI",
    "韩国KOSPI200": "KOSPI200",
    # 港股指数：`513130`（恒生科技，成分只有一只港股 ETF `3033`）走指数代理时可直接用
    # 恒生科技指数 —— 成分虽是港股，但它持有的是**另一只 ETF 的份额**，用其市价会把
    # 那只 ETF 自身的折溢价算进来（判据 `low_cov`，见 judge_path）。
    "恒生科技": "HSTECH", "恒生指数": "HSI", "恒生中国企业": "HSCEI",
    # 以下 **P1 无源**（东财未找到 secid / 属自定义主题指数）⇒ 显式留 None，不猜：
    "东证指数": None, "中韩半导体": None, "美国50": None,
    "纳斯达克科技市值加权": None, "纳斯达克生物科技": None,
    "标普石油天然气勘探及生产": None, "标普生物科技精选行业指数": None,
    "标普500消费精选指数": None, "中证全球中国互联网指数": None,
    "中证海外中国互联网30指数": None, "中国互联网50": None,
    "新交所新兴亚洲精选50指数": None, "新交所泛东南亚科技指数": None,
    "富时亚太低碳精选": None, "中证全球中国教育主题指数": None,
    "标普中国新经济行业指数": None,
}

# 深市虚拟「申赎现金」行 —— 不是持仓，判分流时必须排除。
VIRTUAL_CASH_CODE = "159900"

# 迁移：LOF / 现金管理 类别 → jsl_market（与 ETF 共用 'A' 实现「亚洲 Tab 混合展示」）
_LOF_UNIVERSE = "lof_universe.tsv"
_LOF_CAT_TO_MARKET = {"国内LOF": "L", "QDII亚洲": "A", "现金管理": "C"}


def _conn():
    return sqlite3.connect(DB_PATH)


def _guess_fx(index_nm):
    s = index_nm or ""
    for kw, pair in _FX_BY_INDEX:
        if kw in s:
            return pair
    return None


def pcf_source_of(code):
    """取数通道：沪市 = 上交所 XML，深市 = 深交所静态宽表。"""
    return "sse_xml" if str(code)[:1] == "5" else "szse_txt"


def read_jsl_tsv(market):
    """读集思录快照 → list[dict]（键 = 英文列名 + `jsl_market`）。"""
    path = os.path.join(DATA_DIR, JSL_FILES[market])
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            code = (r.get("code") or "").strip()
            if not code:
                continue
            r = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            r["jsl_market"] = market
            out.append(r)
    return out


def load_pool():
    """两表合并 → list[dict]（246 只）。"""
    return read_jsl_tsv("A") + read_jsl_tsv("E")


# ----------------------------------------------------------------------
# 建档
# ----------------------------------------------------------------------
def ensure_profiles(verbose=True):
    """把两表落成 `fund_profile` + `fund_iopv_config`（enabled=0）seed。幂等。

    · `fund_profile`   —— 展示列全量 upsert（集思录快照为准）。
    · `fund_iopv_config` —— `INSERT OR IGNORE`，**绝不覆盖**已有配置（含已接入的 3 只生产标的）。
    返回统计 dict。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = _conn()
    stat = {"profiles": 0, "config_new": 0, "config_skip": 0, "by_market": Counter()}
    for mkt, fn in JSL_FILES.items():
        for r in read_jsl_tsv(mkt):
            code = r["code"]
            c.execute(
                """INSERT INTO fund_profile
                   (fund_code, jsl_market, fund_name, company, jsl_index_nm,
                    apply_fee, apply_status, redeem_fee, redeem_status, mgmt_fee, updated_at,
                    fund_type, category, val_method)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fund_code) DO UPDATE SET
                     jsl_market=excluded.jsl_market, fund_name=excluded.fund_name,
                     company=excluded.company, jsl_index_nm=excluded.jsl_index_nm,
                     apply_fee=excluded.apply_fee, apply_status=excluded.apply_status,
                     redeem_fee=excluded.redeem_fee, redeem_status=excluded.redeem_status,
                     mgmt_fee=excluded.mgmt_fee, updated_at=excluded.updated_at,
                     fund_type=excluded.fund_type, category=excluded.category,
                     val_method=excluded.val_method""",
                (code, mkt, r.get("name"), r.get("company"), r.get("index"),
                 r.get("sub_fee"), r.get("sub_status"), r.get("red_fee"),
                 r.get("red_status"), r.get("mgmt_fee"), now,
                 "ETF", "跨境ETF", (r.get("index") or "").strip() and None))
            stat["profiles"] += 1
            stat["by_market"][mkt] += 1

            exists = c.execute("SELECT 1 FROM fund_iopv_config WHERE fund_code=?",
                               (code,)).fetchone()
            if exists:
                stat["config_skip"] += 1
                continue
            c.execute(
                """INSERT INTO fund_iopv_config
                   (fund_code, fund_name, kind, pcf_source, fx_currency, fx_rate,
                    creation_redemption_unit, quote_source, index_key,
                    premium_threshold_bps, discount_threshold_bps, enabled)
                   VALUES (?,?,?,?,?,NULL,NULL,NULL,?,50,-50,0)""",
                (code, r.get("name"), "跨境ETF", pcf_source_of(code),
                 _guess_fx(r.get("index")),
                 INDEX_KEY_MAP.get((r.get("index") or "").strip())))
            stat["config_new"] += 1
    c.commit()
    c.close()
    if verbose:
        print("fund_profile 已写入 %d 行（亚洲 %d / 欧美 %d）"
              % (stat["profiles"], stat["by_market"]["A"], stat["by_market"]["E"]))
        print("fund_iopv_config 新增 %d 行（enabled=0），已存在跳过 %d 行"
              % (stat["config_new"], stat["config_skip"]))
    return stat


# ----------------------------------------------------------------------
# LOF / 现金管理 建档（2026-09-18 迁移，docs/50）
# ----------------------------------------------------------------------
def ensure_lof_profiles(verbose=True):
    """把 `data/lof_universe.tsv`（一次性从 arbTest 导出）落成 `fund_profile`。幂等。

    · 列：fund_type='LOF'、category、idx_code、idx_name、val_method（lof_index/bond）。
    · jsl_market 按类别映射：国内LOF→'L'、QDII亚洲→'A'（与 ETF 亚洲混合）、现金管理→'C'。
    · **绝不触碰 arb_master.db**：本函数只读本地 tsv，运行时零依赖 arbTest。
    返回写入/跳过统计 dict。"""
    path = os.path.join(DATA_DIR, _LOF_UNIVERSE)
    if not os.path.exists(path):
        raise SystemExit("缺少 %s（请先运行 scripts/export_lof_from_arbtest.py 导出）" % path)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(r)
    c = _conn()
    stat = {"lof": 0, "by_cat": Counter()}
    for r in rows:
        code = (r.get("fund_code") or "").strip()
        cat = (r.get("category") or "").strip()
        if not code:
            continue
        mkt = _LOF_CAT_TO_MARKET.get(cat, "L")
        c.execute(
            """INSERT INTO fund_profile
               (fund_code, jsl_market, fund_name, company, jsl_index_nm,
                fund_type, category, idx_code, idx_name, val_method, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fund_code) DO UPDATE SET
                 jsl_market=excluded.jsl_market, fund_name=excluded.fund_name,
                 category=excluded.category, idx_code=excluded.idx_code,
                 idx_name=excluded.idx_name, val_method=excluded.val_method,
                 updated_at=excluded.updated_at""",
            (code, mkt, r.get("fund_name"), "", r.get("idx_name"),
             "LOF", cat, r.get("idx_code"), r.get("idx_name"),
             r.get("val_method"), now))
        stat["lof"] += 1
        stat["by_cat"][cat] += 1
    c.commit()
    c.close()
    if verbose:
        print("LOF/现金管理 档案已写入 %d 行" % stat["lof"])
        for k, v in stat["by_cat"].items():
            print("  %s: %d" % (k, v))
    return stat


def backfill_index_keys(verbose=True):
    """按 `INDEX_KEY_MAP` 补齐 `fund_iopv_config.index_key`（**只填空值，绝不覆盖**）。

    ⚠️ 513000 的生产配置 `index_key='N225'` 已存在 ⇒ 不会被改动。
    返回补写条数。"""
    c = _conn()
    n = 0
    for code, nm in c.execute("SELECT fund_code, jsl_index_nm FROM fund_profile"):
        key = INDEX_KEY_MAP.get((nm or "").strip())
        if not key:
            continue
        cur = c.execute("SELECT index_key FROM fund_iopv_config WHERE fund_code=?",
                        (code,)).fetchone()
        if cur and (cur[0] or "").strip():
            continue
        c.execute("UPDATE fund_iopv_config SET index_key=? WHERE fund_code=?", (key, code))
        n += 1
    c.commit()
    c.close()
    if verbose:
        print("index_key 补齐 %d 行" % n)
    return n


# ----------------------------------------------------------------------
# 分流判定
# ----------------------------------------------------------------------
def judge_path(comps):
    """按成分市场结构判定 IOPV 通道（**UI 不展示**）。

    comps: `[{'stock_code': str, 'market': str}, ...]`（来自 pcf_component，含现金行）
    返回 `(quote_path, n_market_str, ncomp, low_cov)`

    🔴 判据（2026-09-17 修订，docs/40 §6.5）：**成分全部在港股挂牌 ⇒ 逐券**，
    **不再附加「成分数 ≥ 5」**。`low_cov`（n<5）只作为信息标签保留，不参与分流。

    为什么去掉 n≥5：单券 FoF（成分只列 1 只**港股上市 ETF**，如 513130 持 `3033`
    恒生科技ETF、520830/159329 持 `2830` CSOP Saudi Arabia ETF）——**官方 IOPV 用的
    就是该 ETF 的市价**。实证：513130 反解隐含汇率 = **0.85493**，与全体正常标的的
    0.855 同档 ⇒ 官方**没有**任何「剔除成分自身折溢价」的处理；改用指数代理反而
    引入 **+77bp** 误差（用 HSTECH 指数 vs 用 3033 市价，两条口径不同）。
    ⇒ 判据只看「成分能否取到实时港股价」，不看成分数量。
    真正该走代理的是成分**在境外挂牌**的那些（`1330`/`1321`/`1473`/`BOVB11`… 无港股行情）。
    """
    real = [x for x in comps
            if str(x.get("stock_code") or "").strip() != VIRTUAL_CASH_CODE]
    n = len(real)
    cnt = Counter((str(x.get("market") or "").strip() or "?") for x in real)
    n_market = " ".join("%s×%d" % (k, v) for k, v in cnt.most_common())
    low_cov = 1 if n < 5 else 0
    other = set(cnt) - {"HK"}
    path = "perstock" if not other else "proxy"
    return path, n_market, n, low_cov


def _read_components(c, code):
    """取该基金**最新一份公告**的成分（一次查询，含 market）。"""
    td = c.execute("SELECT MAX(trade_date) FROM pcf_component WHERE fund_code=?",
                   (code,)).fetchone()
    if not td or not td[0]:
        return None, None
    rows = c.execute(
        "SELECT stock_code, market FROM pcf_component WHERE fund_code=? AND trade_date=?",
        (code, td[0])).fetchall()
    return td[0], [{"stock_code": a, "market": b} for a, b in rows]


def classify(codes=None, fetch=True, sleep=0.25, verbose=True):
    """抓 PCF（或读库最新一份）→ 判定分流 → 回写 `fund_profile`。

    · `fetch=True`：走 `PCFFetcher.fetch_and_store`（**落库唯一入口**），
      缺 PCF 的先抓；已抓过的当日快照会跳过（`check_today_complete`）。
    · 抓取失败不兜底：记入返回结果的 `failed` 列表，该只 `quote_path` 保持原值。

    返回 `{'done': [...], 'failed': [(code, err), ...]}`（后台任务用，可打印进度）。
    """
    from iopv_pcf import PCFFetcher, PCFUnavailable
    c = _conn()
    if codes is None:
        codes = [r[0] for r in c.execute(
            "SELECT fund_code FROM fund_profile ORDER BY jsl_market, fund_code")]
    done, failed = [], []
    fetcher = PCFFetcher()
    for i, code in enumerate(codes, 1):
        try:
            has = c.execute("SELECT COUNT(*) FROM pcf_component WHERE fund_code=?",
                            (code,)).fetchone()[0]
            if fetch and has == 0:
                sid, cnt = fetcher.fetch_and_store(code)
                if verbose:
                    print("  [%d/%d] %s 抓取 PCF → %d 条成分" % (i, len(codes), code, cnt))
                if sleep:
                    import time as _t
                    _t.sleep(sleep)
            td, comps = _read_components(c, code)
            if not comps:
                failed.append((code, "无 PCF 成分"))
                if verbose:
                    print("  [%d/%d] %s ⚠️ 无 PCF 成分" % (i, len(codes), code))
                continue
            path, n_market, n, low = judge_path(comps)
            c.execute(
                """UPDATE fund_profile SET quote_path=?, n_market=?, ncomp=?,
                   low_cov=?, classified_at=? WHERE fund_code=?""",
                (path, n_market, n, low,
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code))
            # 🔴 同步估值配置的通道：`index_proxy` 才写，逐券留 NULL（引擎默认走逐券）。
            #   看板侧其实按 `fund_profile.quote_path` 直接分派，这里同步是为了
            #   `fund_iopv_config` 自身**如实反映**该券走哪条通道（审计用，非计算依赖）。
            c.execute("UPDATE fund_iopv_config SET quote_source=? WHERE fund_code=?",
                      ("index_proxy" if path == "proxy" else None, code))
            c.commit()
            done.append({"fund_code": code, "path": path, "ncomp": n,
                         "n_market": n_market, "low_cov": low, "trade_date": td})
        except Exception as e:
            failed.append((code, str(e)))
            if verbose:
                print("  [%d/%d] %s ❌ %s" % (i, len(codes), code, e))
    c.close()
    return {"done": done, "failed": failed}


def summary():
    """分流结果概览（按 path × jsl_market）。"""
    c = _conn()
    rows = c.execute(
        "SELECT jsl_market, quote_path, COUNT(*) FROM fund_profile "
        "GROUP BY jsl_market, quote_path ORDER BY jsl_market, quote_path").fetchall()
    tot = c.execute("SELECT COUNT(*), SUM(quote_path IS NULL) FROM fund_profile").fetchone()
    c.close()
    return {"groups": rows, "total": tot[0], "unclassified": tot[1] or 0}


def list_profiles(market=None, path=None):
    """读看板档案。"""
    c = _conn()
    sql = ("SELECT fund_code, jsl_market, fund_name, company, jsl_index_nm, "
           "apply_fee, apply_status, redeem_fee, redeem_status, mgmt_fee, "
           "quote_path, n_market, ncomp, low_cov FROM fund_profile")
    cond, args = [], []
    if market:
        cond.append("jsl_market=?")
        args.append(market)
    if path:
        cond.append("quote_path=?")
        args.append(path)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY jsl_market, fund_code"
    rows = c.execute(sql, args).fetchall()
    c.close()
    cols = ["fund_code", "jsl_market", "fund_name", "company", "jsl_index_nm",
            "apply_fee", "apply_status", "redeem_fee", "redeem_status", "mgmt_fee",
            "quote_path", "n_market", "ncomp", "low_cov"]
    return [dict(zip(cols, r)) for r in rows]


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ensure_profiles()
    print("\n===== 分流结果汇总 =====")
    s = summary()
    for mkt, path, n in s["groups"]:
        print("  %s(%s)  %-9s %d 只" % (mkt, JSL_MARKET_NAME.get(mkt, "?"),
                                        path or "(未分类)", n))
    print("  合计 %d 只，未分类 %d 只" % (s["total"], s["unclassified"]))


if __name__ == "__main__":
    main()
