# -*- coding: utf-8 -*-
"""铁矿石图表数据层：读 data/ironore_series.json，按年数切片后交给前端。

数据由 `scripts/build_ironore_series.py` 生成（反推现货 + 生意社年度对比）。
本模块只读不写，带进程内缓存（按文件 mtime 失效）。

对外：
    payload(years=3)  →  {ok, variety, name, unit, series, annual, meta}
    clear_cache()     →  页面「更新数据」后调用（重算了 json 要重新读）
"""
import datetime
import json
import os

from config import ROOT

SERIES_JSON = os.path.join(ROOT, "data", "ironore_series.json")

_CACHE = {"mtime": None, "data": None}


def _load():
    if not os.path.exists(SERIES_JSON):
        return None
    mtime = os.path.getmtime(SERIES_JSON)
    if _CACHE["data"] is not None and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    with open(SERIES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    _CACHE["mtime"] = mtime
    _CACHE["data"] = data
    return data


def payload(years=3):
    """years: 1 / 3 / 0(=全部)。默认 3 年。"""
    d = _load()
    if not d:
        return {"ok": False,
                "error": "铁矿石序列文件不存在，请先跑 scripts/build_ironore_series.py"}
    series = d.get("series", [])
    if years and years > 0 and series:
        cutoff = (datetime.date.today()
                  - datetime.timedelta(days=int(365.25 * years))).isoformat()
        series = [r for r in series if r["date"] >= cutoff]
    return {
        "ok": True,
        "variety": "i",
        "name": "铁矿石",
        "unit": "元/吨",
        "series": series,
        "annual": d.get("annual", {}),
        "meta": d.get("meta", {}),
    }


def clear_cache():
    _CACHE["mtime"] = None
    _CACHE["data"] = None
