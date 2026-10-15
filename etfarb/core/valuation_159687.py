# -*- coding: utf-8 -*-
"""
159687 静态估值 - 完整版（复用 arbTest 共享库）

架构：
1. 数据源复用 arbTest 的 data_fetcher（东财净值）+ database（汇率）
2. 静态估值算法复用 arbTest 的 static_valuation.py 逻辑
3. LCU 价格源：富途 OpenD（需确认代码格式）或 Yahoo Finance

使用：
  python -m etf_arb.valuation_159687 --mode mock    # Mock 测试
  python -m etf_arb.valuation_159687 --mode real    # 真实数据（需配置 LCU 价格源）
"""

import argparse
import sqlite3
import sys
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

# ---- 路径：无硬编码本机路径（clone 即可跑）----
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 注入 LOFarb/src（arbcore 复用），逻辑与 core/db.py:ensure_arbcore_on_path 一致
#   [AI-2026-10-07] 原写死 "../../LOFarb/src"，ETFarb 迁入 src/ 后算成 ETFarb/LOFarb/src 而失效；
#   改走 core/db.py:find_sibling_path（布局无关，单一真源）。
import db as _db  # noqa: E402
ARBTEST_PATH = None
for _p in (
    _os.environ.get("ARBCORE_SRC") or _os.environ.get("LOFARB_SRC"),
    _db.find_sibling_path("LOFarb", "src"),
    "/home/ubuntu/arbtest/src" if _os.name != "nt" else None,
):
    if _p and _os.path.isdir(_p):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        ARBTEST_PATH = _p
        break

# ============ 导入共享模块 ============
try:
    from arbcore.fetchers.data_fetcher import DataFetcher
    from arbcore.calculators.static_valuation import StaticValuationCalculator
    SHARED_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] 无法导入 LOFarb 共享模块: {e}")
    SHARED_AVAILABLE = False

# ============ 配置 ============
from db import DB_PATH  # noqa: E402  复用 core/db.py 统一共享库解析（ARB_MASTER_DB 环境变量 / 兄弟目录回退）
FUND_CODE = "159687"
LCU_SYMBOL = "LCU"  # 新加坡交易所
POSITION = 1.0  # 仓位假设（后续可从 woody 取）


# ============ 数据获取 ============

def get_159687_nav_history() -> List[Dict]:
    """获取 159687 历史净值（东财接口）"""
    if not SHARED_AVAILABLE:
        return []

    fetcher = DataFetcher()
    nav_dict = fetcher.fetch_lof_nav_data(FUND_CODE)

    return [{'date': d, 'nav': v} for d, v in nav_dict.items()]


def get_fx_history() -> Dict[str, float]:
    """获取 USD/CNY 中间价历史（数据库）"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "SELECT date, usd_cny_mid FROM exchange_rate ORDER BY date DESC LIMIT 60"
        )
        return {r[0]: r[1] for r in cursor.fetchall()}
    finally:
        conn.close()


def get_lcu_price(date_str: str = None) -> Optional[float]:
    """
    获取 LCU 价格（优先富途，备选 Yahoo）

    TODO: 实现 LCU 价格获取
    - 富途：需确认新加坡股票代码格式（Market.SG）
    - Yahoo：LCU.SI 或 ^LCU-IV（需 VPN）
    - 手动输入：用于测试
    """
    # 临时：返回用户提供的价格
    if date_str == '2026-08-29':
        return 2.20  # 用户手机查到的收盘价

    # TODO: 实现富途 API 调用
    # TODO: 实现 Yahoo Finance API 调用
    return None


# ============ 核心算法 ============

def calculate_static_valuation(
    base_nav: float,
    base_lcu_price: float,
    base_fx: float,
    curr_lcu_price: float,
    curr_fx: float,
    position: float = POSITION,
) -> Optional[float]:
    """
    同步对比法计算静态估值

    公式（与 arbTest static_valuation.py 一致）：
      static_val = base_nav × [1 + position × (LCU_ratio × FX_ratio - 1)]
    """
    if not all([base_nav, base_lcu_price, base_fx, curr_lcu_price, curr_fx]):
        return None

    lcu_ratio = curr_lcu_price / base_lcu_price
    fx_ratio = curr_fx / base_fx

    net_ratio = position * (lcu_ratio * fx_ratio - 1.0)
    static_val = base_nav * (1.0 + net_ratio)

    return round(static_val, 4)


# ============ 对照表生成 ============

def generate_comparison_table() -> List[Dict]:
    """生成 159687 静态估值对照表"""
    # 1. 获取 159687 净值历史
    nav_history = get_159687_nav_history()
    if not nav_history:
        print("[ERROR] 无法获取 159687 净值历史")
        return []

    # 2. 获取汇率历史
    fx_history = get_fx_history()

    # 3. 对齐日期
    nav_dict = {r['date']: r['nav'] for r in nav_history}
    common_dates = sorted(set(nav_dict.keys()) & set(fx_history.keys()), reverse=True)

    if len(common_dates) < 2:
        print("[ERROR] 有效日期不足")
        return []

    # 4. 找锚点日（最新有净值的日子）
    anchor_date = common_dates[0]
    anchor_nav = nav_dict[anchor_date]
    anchor_fx = fx_history[anchor_date]

    # 获取锚点日 LCU 价格（需要反推）
    # 公式：anchor_nav ≈ anchor_lcu_price × anchor_fx × conversion_factor
    # conversion_factor = PCF_LCU_QUANTITY / PCF_UNIT_SIZE = 12456 / 100000 = 0.12456
    CONVERSION_FACTOR = 12456 / 100000
    anchor_lcu_price = anchor_nav / (anchor_fx * CONVERSION_FACTOR)

    print(f"[锚点] {anchor_date}: nav={anchor_nav}, fx={anchor_fx}, lcu={anchor_lcu_price:.4f}")

    # 5. 生成对照表
    results = []
    for date in common_dates[:10]:  # 最近 10 天
        curr_nav = nav_dict.get(date)
        curr_fx = fx_history.get(date)
        curr_lcu = get_lcu_price(date)

        if curr_nav and curr_fx and curr_lcu and anchor_lcu_price:
            static_val = calculate_static_valuation(
                anchor_nav, anchor_lcu_price, anchor_fx,
                curr_lcu, curr_fx, POSITION
            )

            error_bp = None
            note = ''
            if static_val and curr_nav:
                error_bp = round((static_val - curr_nav) / curr_nav * 10000, 1)
                if abs(error_bp) > 50:
                    note = '[!] 误差大'
                elif abs(error_bp) > 20:
                    note = '[+] 偏大'

            results.append({
                'date': date,
                'anchor_nav': anchor_nav,
                'anchor_lcu': anchor_lcu_price,
                'anchor_fx': anchor_fx,
                'curr_lcu': curr_lcu,
                'curr_fx': curr_fx,
                'static_val': static_val,
                'actual_nav': curr_nav,
                'error_bp': error_bp,
                'note': note,
            })

    return results


# ============ 输出 ============

def print_table(results: List[Dict]):
    """打印对照表"""
    if not results:
        print("[No data]")
        return

    print("\n" + "=" * 110)
    print("159687 静态估值对照表（同步对比法）")
    print("=" * 110)
    print(f"{'日期':<12} {'锚点净值':<10} {'锚点LCU':<10} {'锚点汇率':<8} {'当前LCU':<10} {'当前汇率':<8} {'静态估值':<10} {'实际净值':<10} {'误差(bp)':<10} {'备注'}")
    print("-" * 110)

    for r in results:
        print(
            f"{r['date']:<12} "
            f"{r['anchor_nav']:<10.4f} "
            f"{r['anchor_lcu']:<10.4f} "
            f"{r['anchor_fx']:<8.4f} "
            f"{r['curr_lcu']:<10.4f} "
            f"{r['curr_fx']:<8.4f} "
            f"{r['static_val']:<10.4f} "
            f"{r['actual_nav']:<10.4f} "
            f"{r['error_bp']:<+10.1f} "
            f"{r['note']}"
        )

    print("=" * 110)

    # 统计
    errors = [r['error_bp'] for r in results if r['error_bp'] is not None]
    if errors:
        print(f"\n误差统计：")
        print(f"  平均误差: {sum(errors)/len(errors):+.1f} bp")
        print(f"  最大误差: {max(abs(e) for e in errors):.1f} bp")
        print(f"  误差>50bp: {sum(1 for e in errors if abs(e) > 50)} 天")


def save_to_csv(results: List[Dict], filename: str = "159687_static_valuation.csv"):
    """保存为 CSV"""
    import csv
    with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['date', 'anchor_nav', 'anchor_lcu', 'anchor_fx',
                                                'curr_lcu', 'curr_fx', 'static_val', 'actual_nav',
                                                'error_bp', 'note'])
        writer.writeheader()
        writer.writerows(results)
    print(f"\n已保存到: {filename}")


# ============ 主函数 ============

def main():
    parser = argparse.ArgumentParser(description='159687 静态估值对照表生成器')
    parser.add_argument('--csv', action='store_true', help='保存为 CSV')
    args = parser.parse_args()

    print("\n" + "=" * 110)
    print("159687 静态估值测试（同步对比法）")
    print("=" * 110)
    print(f"算法: static_val = base_nav × [1 + position × (LCU_ratio × FX_ratio - 1)]")
    print(f"假设仓位: {POSITION*100:.0f}%")
    print(f"换算系数: 12456/100000 = {12456/100000:.4f}")

    # 生成对照表
    results = generate_comparison_table()

    # 打印
    print_table(results)

    # 保存 CSV
    if args.csv:
        save_to_csv(results)

    # 分析
    print("\n" + "=" * 110)
    print("分析结论：")
    errors = [r['error_bp'] for r in results if r['error_bp'] is not None]
    if errors:
        avg_err = sum(errors) / len(errors)
        max_err = max(abs(e) for e in errors)
        print(f"  - 平均误差: {avg_err:+.1f} bp")
        print(f"  - 最大误差: {max_err:.1f} bp")
        if max_err < 20:
            print("  [OK] 算法准确，可用于实盘估值")
        elif max_err < 50:
            print("  [WARN] 算法基本可用，但需关注大误差日")
        else:
            print("  [ERROR] 误差较大，可能需要：")
            print("     1. 改用 LCU 净值（而非价格）")
            print("     2. 调整仓位参数")
            print("     3. 检查汇率取值（中间价 vs 在岸价）")
    print("=" * 110 + "\n")


if __name__ == "__main__":
    main()
