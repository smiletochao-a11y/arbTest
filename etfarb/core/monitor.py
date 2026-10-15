# -*- coding: utf-8 -*-
"""本地监控入口：循环拉数据 → 计算 IOPV → 打印信号。

默认用 MockDataSource（本地可直接运行，演示用）。
接入真实行情时换 QMTDataSource（需在大QMT 策略环境内，或后续自己写数据源）。

用法：
    python -m etf_arb.monitor                 # 用 Mock 数据跑一轮
    python -m etf_arb.monitor --scenario premium
    python -m etf_arb.monitor --once          # 只跑一轮就退出
"""


import argparse
import sys
import time

from .config import (
    DEFAULT_COST, DISCOUNT_THRESHOLD_BPS, PREMIUM_THRESHOLD_BPS,
    REFRESH_SECONDS, WATCHLIST,
)
from .data_source import DataSource, MockDataSource
from .iopv_engine import evaluate, format_signal
from .utils import now_str, print_basket_summary


def scan(ds: DataSource, cost=None, threshold_premium=None, threshold_discount=None) -> None:
    cost = cost or DEFAULT_COST
    threshold_premium = PREMIUM_THRESHOLD_BPS if threshold_premium is None else threshold_premium
    threshold_discount = DISCOUNT_THRESHOLD_BPS if threshold_discount is None else threshold_discount

    print(f"\n===== {now_str()} ETF申赎套利监控 =====")
    rows = []
    for code, name, kind in WATCHLIST:
        info = ds.get_etf_info(code)
        if info is None:
            rows.append([code, name, kind, "无申赎清单"])
            continue
        quotes = ds.get_quotes([c.code for c in info.components])
        etf_price = ds.get_etf_price(code)
        official = ds.get_official_iopv(code)
        sig = evaluate(info, quotes, etf_price, cost, official)

        flag = ""
        if sig.direction == "premium" and sig.premium >= threshold_premium:
            flag = "★申购套利"
        elif sig.direction == "discount" and sig.premium <= threshold_discount:
            flag = "★赎回套利"
        elif sig.direction != "none":
            flag = "o 有毛利润(未过阈值)"
        rows.append([
            code, name, kind,
            f"申{sig.iopv_creation:.4f}",
            f"赎{sig.iopv_redemption:.4f}",
            f"中{sig.iopv_mid:.4f}",
            f"官{official if official is not None else '-'}",
            f"{sig.premium:+.1f}bp",
            f"{sig.pnl_bps:+.1f}bp",
            f"{sig.cash_replace_ratio*100:.1f}%",
            flag,
        ])
        for r in sig.reasons:
            print(f"  ! {code} {r}")
    print_basket_summary(
        [["代码", "名称", "类型", "IOPV申", "IOPV赎", "IOPV中", "官方IOPV", "溢价", "净盈亏", "替代%", "信号"]] + rows
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal",
                    choices=["normal", "premium", "discount", "limit_down", "halted"])
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    args = ap.parse_args()

    ds = MockDataSource(scenario=args.scenario)
    try:
        while True:
            scan(ds)
            if args.once:
                break
            time.sleep(REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("\n监控已停止")


if __name__ == "__main__":
    main()
