"""
Pydantic 数据模型 - API 请求/响应结构
"""
from pydantic import BaseModel, Field
from typing import Optional, List


class BondVO(BaseModel):
    """可转债视图对象 - 返回给前端"""
    bond_code: str = ""
    bond_name: str = ""
    stock_code: str = ""
    stock_name: str = ""
    bond_price: float = 0.0
    bond_change_pct: float = 0.0
    stock_price: float = 0.0
    stock_change_pct: float = 0.0
    convert_price: float = 0.0
    convert_value: float = 0.0
    premium_rate: float = 0.0
    double_low: float = 0.0
    pre_tax_yield: float = 0.0
    pre_tax_put_yield: float = 0.0
    remaining_years: float = 0.0
    remaining_size: float = 0.0
    maturity_date: str = ""
    credit_rating: str = ""
    pure_bond_value: float = 0.0
    put_value: float = 0.0
    elasticity: float = 0.0
    pb_ratio: float = 0.0
    redeem_trigger_price: float = 0.0
    put_trigger_price: float = 0.0
    turnover_rate: float = 0.0
    turnover_amount: float = 0.0
    historical_volatility: float = 0.0
    ma20_premium_rate: float = 0.0
    theoretical_value: float = 0.0
    deviation: float = 0.0
    data_date: str = ""
    source: str = ""


class StrategyResultVO(BaseModel):
    """策略筛选结果"""
    strategy_name: str = ""
    strategy_key: str = ""
    bonds: List[BondVO] = []
    filter_desc: str = ""
    data_date: str = ""


class DataStatusVO(BaseModel):
    """数据状态"""
    data_date: str = ""
    source: str = ""
    total_count: int = 0
    updated_at: str = ""
