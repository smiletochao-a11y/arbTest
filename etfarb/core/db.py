"""ETFarb 数据层 —— 单库方案（2026-10-05 拍板）。

设计红线（东哥 2026-10-05 拍板）：
- arbTest（交易程序）与 ETFarb（展示程序）**同库**：共用 `arb_master.db` 的
  宽表 `unified_fund_history` 承载全部 414 只的原始行情 + arbTest 派生估值。
- ETFarb 后端连接**直接指向** arb_master.db（与 arbTest 同一文件，绝不另建库）。
- 各写各列、互不碰对方的派生列（等价原「绝不反向写」红线）。
  写助手 `upsert_fund_history` 只动指定列、绝不整行替换，避免清掉 arbTest 在
  重叠 48 只上写的 static_val/index_close/calibration 等派生列。
- 实时值（rt_val/rt_premium）内存现算**不落库**，宽表无 rt 列写入。

历史碎片表（fund_nav / fund_share / fund_nav_history）已并入 unified_fund_history
（见 data/_migrate_fragments.py），不再建、不再读写，验证后删除。
"""
import os
import sys
import sqlite3

# ---- 路径 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(_HERE), "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _ancestor_dirs(levels: int = 8):
    """自 core/ 目录起逐级向上产出祖先目录（含自身），到盘根为止。"""
    cur = os.path.abspath(_HERE)
    out = []
    for _ in range(levels):
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def find_sibling_path(*parts):
    """逐级上溯祖先，找「兄弟目录下的资源」，**绝不写死层数**。

    [AI-2026-10-07] ETFarb 迁入 `src/` 后（`ETFarb/core` → `ETFarb/src/core`），原先写死的
    `../../LOFarb/src`、`../../_shared/...` 全部算错（变成 `ETFarb/LOFarb/...`）→ 静默失效。
    改为自 core/ 逐级上溯：无论 ETFarb 位于 `<ARB>/ETFarb/core` 还是 `<ARB>/ETFarb/src/core`，
    都能命中 `<ARB>/LOFarb/src`、`<ARB>/_shared/arb_master.db`。

    例：`find_sibling_path("LOFarb", "src")` / `find_sibling_path("_shared", "arb_master.db")`。
    找不到返回 None（不兜底、不猜测）。
    """
    for base in _ancestor_dirs():
        cand = os.path.join(base, *parts)
        if os.path.exists(cand):
            return cand
    return None


def _resolve_unified_db_path():
    """解析共享单库 arb_master.db（与 LOFarb 同一文件）。

    解析顺序（无硬编码本机路径，clone 即可跑）：
      ① ARB_MASTER_DB 环境变量（首选，本机/ARM 各自指定）；
      ② 向上遍历祖先目录找含 database/arb_master.db（库置于本程序树下时）；
      ③ 兄弟目录回退（逐级上溯，不写死层数）：`<ARB>/_shared/arb_master.db`、
         `<ARB>/LOFarb/database/arb_master.db`；
      ④ ARM 标准部署 /home/ubuntu/arbtest/database/arb_master.db（仅非 Windows）。
    """
    env = os.environ.get("ARB_MASTER_DB")
    if env:
        return env
    here = os.path.abspath(_HERE)
    cur = here
    while True:
        cand = os.path.join(cur, "database", "arb_master.db")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for parts in (("_shared", "arb_master.db"), ("LOFarb", "database", "arb_master.db")):
        cand = find_sibling_path(*parts)
        if cand:
            return cand
    if os.name != "nt":
        arm = "/home/ubuntu/arbtest/database/arb_master.db"
        if os.path.exists(arm):
            return arm
    raise FileNotFoundError(
        "找不到 arb_master.db：请设置环境变量 ARB_MASTER_DB，或将共享库置于 "
        "LOFarb/database/ 或 _shared/ 下（与 ETFarb 同级）。"
    )


DB_PATH = _resolve_unified_db_path()


def ensure_arbcore_on_path():
    """注入 LOFarb/src 到 sys.path，使 import arbcore 可用（复用其历史净值取数）。

    仅注入路径，绝不 import arbcore 的写库/调度模块。解析顺序（无硬编码本机路径）：
      ① ARBCORE_SRC / LOFARB_SRC 环境变量；
      ② 逐级上溯找兄弟目录 `<ARB>/LOFarb/src`（`find_sibling_path`，ETFarb 位于
         `<ARB>/ETFarb/core` 或 `<ARB>/ETFarb/src/core` 均能命中）；
      ③ ARM 标准部署 /home/ubuntu/arbtest/src。
    """
    env_src = os.environ.get("ARBCORE_SRC") or os.environ.get("LOFARB_SRC")
    cands = []
    if env_src:
        cands.append(env_src)
    _sib = find_sibling_path("LOFarb", "src")   # [AI-2026-10-07] 布局无关，替代写死的 ../../LOFarb/src
    if _sib:
        cands.append(_sib)
    if os.name != "nt":
        cands.append("/home/ubuntu/arbtest/src")
    for _p in cands:
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.insert(0, _p)


ensure_arbcore_on_path()


def get_conn():
    """返回 arb_master.db 连接（每次新建，避免多线程/多进程共享连接）。
    设 WAL + busy_timeout，防与 arbTest 同文件并发写锁。
    """
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=180000")
    return conn


def ensure_schema():
    """确保 ETFarb 自有展示表存在（幂等）。
    不再建 fund_nav / fund_share / fund_nav_history（已并入 unified_fund_history）。
    unified_fund_history 由 arb_master 共享库提供，假定已存在。
    """
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS index_history (
            symbol TEXT,
            date    TEXT,
            close   REAL,
            source  TEXT DEFAULT 'eastmoney',
            PRIMARY KEY (symbol, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS access_sync_status (
            sync_date     TEXT,
            access_source TEXT,
            PRIMARY KEY (sync_date, access_source)
        )"""
    )
    # ---- 跨境 ETF 看板（2026-09-17 立项，docs/40） ----
    # fund_profile：看板**展示档案**（集思录同名列 + 内部分流结果）。
    #   🔴 职责边界：本表**不存任何参与 IOPV 计算的输入** —— 估值配置在 fund_iopv_config
    #      （IOPVCalculator 的唯一入口），两表不重叠、不互为备份。
    #      这里只有「显示什么」与「怎么分流」，没有「怎么算」。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fund_profile (
            fund_code    TEXT PRIMARY KEY,
            jsl_market   TEXT,     -- 集思录分组：'A'=亚洲 / 'E'=欧美（实测 = 净值基准日 D 的划分）
            fund_name    TEXT,
            company      TEXT,
            jsl_index_nm TEXT,     -- 集思录「相关指数」原文（展示用）
            apply_fee    TEXT, apply_status  TEXT,
            redeem_fee   TEXT, redeem_status TEXT,
            mgmt_fee     TEXT,
            -- 🔴 内部分流（**UI 不展示**，仅供代码选路径）：
            quote_path   TEXT,     -- 'perstock'=逐券 / 'proxy'=指数代理
            n_market     TEXT,     -- 成分市场构成快照，如 'HK×48' / 'OS×1'
            ncomp        INTEGER,  -- PCF 组合证券数（排除 159900 现金行）
            low_cov      INTEGER,  -- 1 = 低覆盖（成分数 < 5 ⇒ 单券 FoF）
            classified_at TEXT,
            updated_at   TEXT,
            -- 🔴 迁移列（2026-09-18：国内LOF / QDII亚洲 / 现金管理 并入通用看板）
            --   fund_type: 'ETF'=跨境ETF（走 IOPV 模型）/ 'LOF'=LOF 或现金管理（走指数跟踪估值）
            --   category : 跨境ETF / 国内LOF / QDII亚洲 / 现金管理
            --   idx_code : LOF 跟踪指数原始代码（估值用）；ETF 该列留空
            --   val_method: perstock / proxy（ETF 沿用）/ lof_index（指数跟踪）/ bond（债券/主动型，估值=净值）
            fund_type   TEXT DEFAULT 'ETF',
            category    TEXT,
            idx_code    TEXT,
            idx_name    TEXT,
            val_method  TEXT
        )"""
    )
    # 迁移列：幂等加列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
    _ALTERS = [
        "ALTER TABLE fund_profile ADD COLUMN fund_type TEXT DEFAULT 'ETF'",
        "ALTER TABLE fund_profile ADD COLUMN category TEXT",
        "ALTER TABLE fund_profile ADD COLUMN idx_code TEXT",
        "ALTER TABLE fund_profile ADD COLUMN idx_name TEXT",
        "ALTER TABLE fund_profile ADD COLUMN val_method TEXT",
        # 2026-09-28：本机早先手工加的列，补进 schema 使 ARM 与本机一致
        # （ARM 缺此列 ⇒ etf_market 装配报 "no such column: in_arbtest"）
        "ALTER TABLE fund_profile ADD COLUMN in_arbtest INTEGER DEFAULT 0",
        # 统一宽表补充 ETFarb 落的来源/时间列（arbTest 不写，留 NULL 不冲突）
        "ALTER TABLE unified_fund_history ADD COLUMN source TEXT",
        "ALTER TABLE unified_fund_history ADD COLUMN fetched_at TEXT",
        # 2026-10-08 修：ARM 宽表缺 acc（累计净值）列 ⇒ E/A ETF 首次抓净值时
        # upsert 写 acc=? 报 "no such column: acc"，整个静态估值采集线程异常退出、
        # E/A 净值历史全没写（本机宽表有此列，两端 schema 不一致）。幂等补齐。
        "ALTER TABLE unified_fund_history ADD COLUMN acc REAL",
        # [AI-2026-10-09] writer 标识列：记录"这行是谁写的"（凌晨批来源排查教训）。
        # 本机手工 ALTER 已加；此处幂等补齐，保证 ARM 等环境自动同步 schema。
        "ALTER TABLE unified_fund_history ADD COLUMN writer TEXT",
    ]
    for sql in _ALTERS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    conn.close()


def upsert_fund_history(fund_code, date, fields, source=None, conn=None, writer=None):
    """写 unified_fund_history 的若干列。

    🔴 安全约束：只动指定列，绝不整行替换（不用 INSERT OR REPLACE），
    避免清掉另一程序（arbTest）在重叠 48 只上写的派生列
    （static_val / index_close / calibration / holding_* 等）。

    - fields: dict{列名: 值}（如 {nav: x, source: 'eastmoney'}、
      {shares: s, shares_added: d, source: 'szse', fetched_at: t}）。
    - 已有 (fund_code,date) 行 → UPDATE 这些列；无则 INSERT（只插指定列 + 主键）。
    - source 可选，若提供则一并写入 source 列。
    - conn 可选：传入则复用该连接（调用方负责 commit/close，适用于批量循环，
      避免每日采集为每只基金每行开连接）；不传则本函数自管连接并提交。
    - [AI-2026-10-10] writer 可选：写入者标识（如 'etfarb:nav_fetcher'），
      供事后追查"这行是谁写的"（凌晨批来源排查教训）。提供则一并写入 writer 列。
    返回受影响行数。
    """
    if not fields:
        return 0
    fields = dict(fields)
    if source is not None:
        fields["source"] = source
    if writer is not None:
        fields["writer"] = writer
    cols = list(fields.keys())
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.cursor()
        set_sql = ", ".join(f"{c}=?" for c in cols)
        cur.execute(
            f"UPDATE unified_fund_history SET {set_sql} "
            f"WHERE fund_code=? AND date=?",
            [fields[c] for c in cols] + [fund_code, date],
        )
        if cur.rowcount == 0:
            all_cols = ["fund_code", "date"] + cols
            ph = ",".join("?" * len(all_cols))
            cur.execute(
                f"INSERT INTO unified_fund_history({','.join(all_cols)}) VALUES({ph})",
                [fund_code, date] + [fields[c] for c in cols],
            )
        if own:
            conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


# 导入即确保表存在
ensure_schema()
