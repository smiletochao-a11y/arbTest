"""
策略引擎 - 老实和尚4策略 + 检钢蹦4类选债
"""
import logging
from typing import List, Dict
from models.schemas import BondVO, StrategyResultVO

logger = logging.getLogger(__name__)

# 通用剔除条件
RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB", "B", "CCC", "CC", "C", "D"]
MIN_RATING = "A-"  # 剔除 A- 以下


def _rating_ok(rating: str) -> bool:
    """评级是否 >= A-"""
    if not rating:
        return False
    try:
        return RATING_ORDER.index(rating) <= RATING_ORDER.index(MIN_RATING)
    except ValueError:
        return False


def _filter_basic(bonds: List[BondVO]) -> List[BondVO]:
    """通用剔除：评级A-以下、到期"""
    result = []
    for b in bonds:
        if not _rating_ok(b.credit_rating):
            continue
        if b.remaining_years <= 0:
            continue
        result.append(b)
    return result


# ========== 老实和尚策略 ==========

def strategy_double_low(bonds: List[BondVO], n: int = 10) -> StrategyResultVO:
    """双低策略：双低值升序取前N"""
    filtered = _filter_basic(bonds)
    filtered.sort(key=lambda x: x.double_low if x.double_low > 0 else 9999)
    selected = filtered[:n]
    return StrategyResultVO(
        strategy_name="双低策略", strategy_key="double_low",
        bonds=selected, filter_desc=f"双低值最低{n}只，剔除评级<A-及到期",
        data_date=bonds[0].data_date if bonds else ""
    )


def strategy_low_premium(bonds: List[BondVO], n: int = 10) -> StrategyResultVO:
    """低溢价策略：溢价率升序取前N"""
    filtered = _filter_basic(bonds)
    filtered.sort(key=lambda x: x.premium_rate if x.premium_rate > 0 else 9999)
    selected = filtered[:n]
    return StrategyResultVO(
        strategy_name="低溢价策略", strategy_key="low_premium",
        bonds=selected, filter_desc=f"溢价率最低{n}只，剔除评级<A-及到期",
        data_date=bonds[0].data_date if bonds else ""
    )


def strategy_low_price(bonds: List[BondVO], n: int = 10) -> StrategyResultVO:
    """低价策略：价格升序取前N"""
    filtered = _filter_basic(bonds)
    filtered.sort(key=lambda x: x.bond_price if x.bond_price > 0 else 9999)
    selected = filtered[:n]
    return StrategyResultVO(
        strategy_name="低价策略", strategy_key="low_price",
        bonds=selected, filter_desc=f"价格最低{n}只，剔除评级<A-及到期",
        data_date=bonds[0].data_date if bonds else ""
    )


def strategy_high_yield(bonds: List[BondVO], n: int = 10) -> StrategyResultVO:
    """高收益率策略：税前收益率降序取前N"""
    filtered = _filter_basic(bonds)
    # 排除剩余年限 < 0.25年的（快到期的债收益率没意义）
    filtered = [b for b in filtered if b.remaining_years >= 0.25]
    filtered.sort(key=lambda x: x.pre_tax_yield, reverse=True)
    selected = filtered[:n]
    return StrategyResultVO(
        strategy_name="高收益率策略", strategy_key="high_yield",
        bonds=selected, filter_desc=f"税前收益率最高{n}只，剔除评级<A-及到期，剩余年限≥0.25年",
        data_date=bonds[0].data_date if bonds else ""
    )


# ========== 检钢蹦策略 ==========

def jgb_micro_cap(bonds: List[BondVO]) -> StrategyResultVO:
    """第1类：微盘转债 - 价格~130，规模<3亿"""
    filtered = _filter_basic(bonds)
    selected = [b for b in filtered if 115 <= b.bond_price <= 145 and 0 < b.remaining_size < 3]
    selected.sort(key=lambda x: x.bond_price)
    return StrategyResultVO(
        strategy_name="检钢蹦-微盘转债", strategy_key="jgb_micro_cap",
        bonds=selected,
        filter_desc="价格115-145，剩余规模<3亿，向下波动有限，弹性高",
        data_date=bonds[0].data_date if bonds else ""
    )


def jgb_low_price_low_premium(bonds: List[BondVO]) -> StrategyResultVO:
    """第2类：低价低溢价波段 - 价格<140，溢价~40%，0.5-5.5年，<10亿"""
    filtered = _filter_basic(bonds)
    selected = [b for b in filtered
                if b.bond_price < 140
                and 20 <= b.premium_rate <= 60
                and 0.5 <= b.remaining_years <= 5.5
                and 0 < b.remaining_size < 10]
    selected.sort(key=lambda x: x.bond_price)
    return StrategyResultVO(
        strategy_name="检钢蹦-低价低溢价", strategy_key="jgb_low_price_low_premium",
        bonds=selected,
        filter_desc="价格<140，溢价20-60%，0.5-5.5年，规模<10亿，波段好手",
        data_date=bonds[0].data_date if bonds else ""
    )


def jgb_defensive(bonds: List[BondVO]) -> StrategyResultVO:
    """第3类：防守型 - 价格<130，溢价~70%，<10亿"""
    filtered = _filter_basic(bonds)
    selected = [b for b in filtered
                if b.bond_price < 130
                and 50 <= b.premium_rate <= 90
                and 0 < b.remaining_size < 10]
    selected.sort(key=lambda x: x.bond_price)
    return StrategyResultVO(
        strategy_name="检钢蹦-防守型", strategy_key="jgb_defensive",
        bonds=selected,
        filter_desc="价格<130，溢价50-90%，规模<10亿，防守为主",
        data_date=bonds[0].data_date if bonds else ""
    )


def jgb_active(bonds: List[BondVO]) -> StrategyResultVO:
    """第4类：活跃品种 - 价格~140，弹性尚可"""
    filtered = _filter_basic(bonds)
    selected = [b for b in filtered
                if 120 <= b.bond_price <= 155
                and b.elasticity > 0.05
                and b.remaining_size < 15]
    selected.sort(key=lambda x: x.elasticity, reverse=True)
    return StrategyResultVO(
        strategy_name="检钢蹦-活跃品种", strategy_key="jgb_active",
        bonds=selected,
        filter_desc="价格120-155，弹性>0.05，规模<15亿，概念丰富",
        data_date=bonds[0].data_date if bonds else ""
    )


# ========== 策略路由 ==========

ALL_STRATEGIES = {
    "double_low": strategy_double_low,
    "low_premium": strategy_low_premium,
    "low_price": strategy_low_price,
    "high_yield": strategy_high_yield,
    "jgb_micro_cap": jgb_micro_cap,
    "jgb_low_price_low_premium": jgb_low_price_low_premium,
    "jgb_defensive": jgb_defensive,
    "jgb_active": jgb_active,
}


def run_strategy(key: str, bonds: List[BondVO], n: int = 10) -> StrategyResultVO:
    """运行指定策略"""
    func = ALL_STRATEGIES.get(key)
    if not func:
        return StrategyResultVO(strategy_name="未知策略", strategy_key=key, filter_desc=f"策略 {key} 不存在")
    if key in ("double_low", "low_premium", "low_price", "high_yield"):
        return func(bonds, n)
    return func(bonds)
