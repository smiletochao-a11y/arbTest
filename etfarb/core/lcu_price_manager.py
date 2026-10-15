# -*- coding: utf-8 -*-
"""
LCU 价格管理模块

用途：
1. 获取 LCU.SI 历史价格（Yahoo Finance，备选）
2. 手动维护 LCU 价格表（权威数据源）
3. 计算 159687 静态估值

数据优先级：
1. 手动维护的 LCU 价格表（最准确）
2. Yahoo Finance API（备选，需 VPN）
3. 用户输入（测试用）
"""

import sys
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Optional

# 添加 LOFarb/src（arbcore 复用），无硬编码本机路径：
#   ① ARBCORE_SRC / LOFARB_SRC 环境变量；② 逐级上溯找 <ARB>/LOFarb/src；③ ARM /home/ubuntu/arbtest/src
#   [AI-2026-10-07] ② 原写死 "../../LOFarb/src"，ETFarb 迁入 src/ 后算成 ETFarb/LOFarb/src 而静默失效；
#   现统一走 core/db.py:find_sibling_path（单一真源，与布局无关）。
_LCU_HERE = os.path.dirname(os.path.abspath(__file__))
if _LCU_HERE not in sys.path:
    sys.path.insert(0, _LCU_HERE)
import db as _db  # noqa: E402  core/db.py：单库解析 + 布局无关的兄弟目录探测
_lcu_injected = False
for _p in (
    os.environ.get("ARBCORE_SRC") or os.environ.get("LOFARB_SRC"),
    _db.find_sibling_path("LOFarb", "src"),
    "/home/ubuntu/arbtest/src" if os.name != "nt" else None,
):
    if _p and os.path.isdir(_p):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        _lcu_injected = True
        break

# ============ 配置 ============
LCU_PRICE_FILE = os.path.join(os.path.dirname(_LCU_HERE), "data", "lcu_prices.json")
LCU_SYMBOL = "LCU.SI"
PCF_LCU_QUANTITY = 12456  # PCF 每最小申赎单位对应 LCU 股数
PCF_UNIT_SIZE = 100000    # 最小申赎单位
CONVERSION_FACTOR = PCF_LCU_QUANTITY / PCF_UNIT_SIZE  # ≈ 0.12456


# ============ 手动价格表 ============

def load_manual_prices() -> Dict[str, float]:
    """加载手动维护的 LCU 价格表"""
    if os.path.exists(LCU_PRICE_FILE):
        try:
            with open(LCU_PRICE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 加载 LCU 价格表失败: {e}")
    return {}


def save_manual_prices(prices: Dict[str, float]):
    """保存手动维护的 LCU 价格表"""
    os.makedirs(os.path.dirname(LCU_PRICE_FILE), exist_ok=True)
    with open(LCU_PRICE_FILE, 'w', encoding='utf-8') as f:
        json.dump(prices, f, indent=2, ensure_ascii=False)
    print(f"[INFO] 已保存 {len(prices)} 条 LCU 价格记录到 {LCU_PRICE_FILE}")


def add_lcu_price(date: str, price: float):
    """添加一条 LCU 价格记录"""
    prices = load_manual_prices()
    prices[date] = price
    save_manual_prices(prices)
    print(f"[INFO] 已添加 LCU 价格: {date} = {price:.4f}")


# ============ Yahoo Finance 获取（备选） ============

def fetch_lcu_from_yahoo() -> Dict[str, float]:
    """从 Yahoo Finance 获取 LCU 历史价格（需 VPN）"""
    try:
        import requests
        from datetime import datetime
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{LCU_SYMBOL}?range=30d&interval=1d"
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        
        lcu_prices = {}
        if data.get('chart', {}).get('result'):
            timestamps = data['chart']['result'][0]['timestamp']
            closes = data['chart']['result'][0]['indicators']['quote'][0]['close']
            for i, ts in enumerate(timestamps):
                date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
                close = closes[i]
                if close:
                    lcu_prices[date] = close
        return lcu_prices
    except Exception as e:
        print(f"[WARN] Yahoo Finance 获取 LCU 价格失败: {e}")
        return {}


# ============ 核心估值算法 ============

def get_lcu_price(date: str) -> Optional[float]:
    """
    获取指定日期的 LCU 价格
    
    优先级：
    1. 手动维护的价格表
    2. Yahoo Finance（如已缓存）
    """
    # 1. 手动价格表
    manual_prices = load_manual_prices()
    if date in manual_prices:
        return manual_prices[date]
    
    # 2. Yahoo 缓存（临时）
    # TODO: 实现缓存机制
    
    return None


def calc_159687_static_valuation(
    anchor_date: str,
    anchor_nav: float,
    anchor_lcu: float,
    anchor_fx: float,
    curr_lcu: float,
    curr_fx: float,
    position: float = 1.0,
) -> Optional[float]:
    """
    同步对比法计算 159687 静态估值
    
    公式：
      static_val = anchor_nav × [1 + position × (LCU_ratio × FX_ratio - 1)]
    """
    if not all([anchor_nav, anchor_lcu, anchor_fx, curr_lcu, curr_fx]):
        return None
    
    lcu_ratio = curr_lcu / anchor_lcu
    fx_ratio = curr_fx / anchor_fx
    net_ratio = position * (lcu_ratio * fx_ratio - 1.0)
    
    return round(anchor_nav * (1.0 + net_ratio), 4)


def calc_159687_inferred_price(static_val: float, fx: float) -> float:
    """
    反推隐含 LCU 价格（用于核对）
    
    公式：
      implied_lcu = static_val / (anchor_nav * (1 + position * (fx_ratio - 1)))
    """
    # 简化版：假设锚点汇率=当前汇率
    # 更精确需要知道锚点汇率
    return static_val / (CONVERSION_FACTOR * fx)


# ============ 测试接口 ============

def test_lcu_valuation():
    """测试 LCU 估值逻辑"""
    print("=" * 70)
    print("LCU 估值测试")
    print("=" * 70)
    
    # 示例数据
    test_data = [
        ('2026-08-27', 1.858, 2.198, 6.784),
        ('2026-08-26', 1.8572, 2.175, 6.7829),
        ('2026-08-25', 1.8405, 2.164, 6.7852),
        ('2026-08-24', 1.834, 2.172, 6.7841),
        ('2026-08-21', 1.8589, 2.17, 6.7817),
    ]
    
    # 使用 8-26 作为锚点（之前测试误差最小）
    anchor = test_data[1]
    print(f"\n锚点日: {anchor[0]}")
    print(f"  NAV={anchor[1]}, LCU={anchor[2]}, FX={anchor[3]}")
    print()
    
    print(f"{'日期':<12} {'当前LCU':<10} {'当前FX':<8} {'静态估值':<10} {'误差(bp)':<10}")
    print("-" * 60)
    
    for row in test_data:
        date, nav, lcu, fx = row
        if date == anchor[0]:
            print(f"{date:<12} {lcu:<10.4f} {fx:<8.4f} {nav:<10.4f} {0:<+10.1f} [ANCHOR]")
            continue
        
        static_val = calc_159687_static_valuation(
            anchor[0], anchor[1], anchor[2], anchor[3],
            lcu, fx, 1.0
        )
        error_bp = (static_val - nav) / nav * 10000 if static_val else 0
        print(f"{date:<12} {lcu:<10.4f} {fx:<8.4f} {static_val:<10.4f} {error_bp:<+10.1f}")
    
    print("=" * 70)


# ============ 主函数 ============

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='LCU 价格管理工具')
    parser.add_argument('--add', nargs=2, metavar=('DATE', 'PRICE'), help='添加 LCU 价格: --add 2026-08-27 2.198')
    parser.add_argument('--test', action='store_true', help='运行测试')
    parser.add_argument('--show', action='store_true', help='显示当前价格表')
    args = parser.parse_args()
    
    if args.add:
        date, price = args.add
        add_lcu_price(date, float(price))
    elif args.test:
        test_lcu_valuation()
    elif args.show:
        prices = load_manual_prices()
        print(f"\n当前 LCU 价格表 ({len(prices)} 条):")
        for date, price in sorted(prices.items(), reverse=True):
            print(f"  {date}: {price:.4f}")
    else:
        parser.print_help()
