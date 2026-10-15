# -*- coding: utf-8 -*-
"""
core/lcu_nav_source.py

LCU (CSOP FTSE Asia Pacific Select Index ETF, SGX 上市) 官方净值「参考源」。

落库（2026-10-08 定调）：
    LCU 净值现已是 159687 的 T-1 估值桥接腿（estimate_t1_lcu 直接消费），
    故落库为正规数据，不再游离于 JSON 文件。
    表：lcu_nav_history(date TEXT PK, nav REAL NOT NULL, source TEXT, fetched_at TEXT)
    库：arb_master.db（与 ETFarb / arbTest 同库）。

红线：
    写入仅由 lcu_nav_fetcher.py 负责（每晚 ARM 爬 CSOP 最新 1 条 UPSERT）。
    本模块只读。index_history / unified_fund_history 一律不动。

接口（与旧 JSON 版保持一致，调用方 estimate_t1_lcu 无需改动）：
    get(date)        -> nav(float) | None
    is_nav_day(date) -> bool
    change(t, prev)  -> ratio | None
    daterange()      -> sorted dates
    lcu_source()     -> 单例
"""

import os
import sqlite3


def _find_db():
    """定位 arb_master.db（零外部依赖，纯路径回退）。

    优先级：ARM 权威库（与 ETFarb / arbTest 同库，db.py 首选）最高，
    避免误命中 /home/ubuntu/database/arb_master.db 等同名库。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    # ARM 权威库（db.py 首选），置顶优先
    cands.append("/home/ubuntu/arbtest/database/arb_master.db")
    cur = here
    for _ in range(8):
        cands += [
            os.path.join(cur, "database", "arb_master.db"),
            os.path.join(cur, "_shared", "arb_master.db"),
            os.path.join(cur, "arb_master.db"),
        ]
        cur = os.path.dirname(cur)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


_DB_PATH = _find_db()
_TABLE = "lcu_nav_history"


class LcuNavSource:
    def __init__(self, db_path=None):
        self.series = {}        # date -> nav(float)
        self.meta = {}
        db = db_path or _DB_PATH
        if not db or not os.path.exists(db):
            print("[lcu_nav_source] 未找到 arb_master.db，LCU 源为空（桥接将降级指数）")
            return
        try:
            con = sqlite3.connect(db)
            rows = con.execute("SELECT date, nav FROM %s" % _TABLE).fetchall()
            con.close()
            for d, nav in rows:
                if nav is not None:
                    self.series[d] = float(nav)
        except Exception as e:
            print("[lcu_nav_source] 读取 %s 失败: %s（桥接将降级指数）" % (_TABLE, e))

    def get(self, date):
        """返回 date 当日 LCU NAV（float）或 None（非估值日/缺失）。"""
        return self.series.get(date)

    def is_nav_day(self, date):
        return date in self.series

    def change(self, date_t, date_prev):
        """返回 LCU[date_t] / LCU[date_prev]；任一日非估值日/缺失返回 None。"""
        a = self.series.get(date_t)
        b = self.series.get(date_prev)
        if a is None or b is None or b == 0:
            return None
        return a / b

    def daterange(self):
        return sorted(self.series.keys())


_LCU = LcuNavSource()


def get_lcu_nav(date):
    return _LCU.get(date)


def lcu_change(date_t, date_prev):
    return _LCU.change(date_t, date_prev)


def lcu_source():
    return _LCU


if __name__ == "__main__":
    s = _LCU
    print("LCU 源点数:", len(s.series))
    if s.series:
        dr = s.daterange()
        print("窗口:", dr[0], "~", dr[-1])
        for d in ("2026-09-29", "2026-09-30", "2026-10-07", "2026-10-01"):
            print(d, "->", s.get(d), "(非估值日)" if not s.is_nav_day(d) else "")
