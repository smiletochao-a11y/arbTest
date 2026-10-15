# -*- coding: utf-8 -*-
"""通用小工具：日志与展示。"""


import datetime
from typing import Iterable, List


def now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def fmt_bps(x: float) -> str:
    """把 bps 格式化成 +12.3bp / -8.0bp 的展示串。"""
    return f"{x:+.1f}bp"


def print_basket_summary(rows: Iterable[List[str]], title: str = "") -> None:
    """简单等宽表格打印（避免依赖第三方库，只做对齐）。短行自动补空。"""
    rows = [list(r) for r in rows]
    if not rows:
        return
    if title:
        print("=" * 8, title, "=" * 8)
    ncol = max(len(r) for r in rows)
    for r in rows:
        if len(r) < ncol:
            r += [""] * (ncol - len(r))
    widths = [max(len(str(r[i])) for r in rows) for i in range(ncol)]
    for r in rows:
        line = "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r))
        print(line)
