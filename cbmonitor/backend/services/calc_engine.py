"""
可转债衍生指标计算引擎
完全自主计算，不依赖任何第三方网站的计算结果

计算项目：
1. 双低值 = 转债价格 + 转股溢价率
2. 税前到期收益率 (YTM) = 现金流 IRR
3. 税前回售收益率 = 回售现金流 IRR
4. 纯债价值 = 未来现金流折现
5. 弹性 = 转股价值 / 转债价格
6. 期权价值 = Black-Scholes 模型
7. 历史波动率 = 正股日收益标准差年化
"""
import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm
from typing import List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


# ========== 基础计算 ==========

def calc_double_low(price: float, premium_rate: float) -> float:
    """
    双低值 = 转债价格 + 转股溢价率
    集思录的标准算法：price + premium_rate (溢价率已经是百分比数字，如 16.9 代表 16.9%)
    所以 double_low = price + premium_rate
    """
    if price <= 0:
        return 0.0
    return price + premium_rate


def calc_elasticity(convert_value: float, bond_price: float) -> float:
    """
    弹性 = 转股价值 / 转债价格
    衡量转债价格对正股变动的敏感度
    弹性=0.8 表示正股涨1%，转债涨0.8%
    """
    if bond_price <= 0:
        return 0.0
    return convert_value / bond_price


def calc_remaining_years(maturity_date: str, today=None) -> float:
    """
    计算剩余年限
    maturity_date: 到期日期字符串
    返回: 剩余年数
    """
    from datetime import datetime
    if today is None:
        today = datetime.now()

    if not maturity_date:
        return 0.0

    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            mat = datetime.strptime(maturity_date.strip(), fmt)
            delta = (mat - today).days
            return round(max(delta / 365.25, 0.0), 4)
        except ValueError:
            continue
    return 0.0


# ========== 现金流构建 ==========

def build_cash_flows(
    remaining_years: float,
    face_value: float = 100.0,
    annual_coupon_rates: Optional[List[float]] = None,
    base_coupon_rate: float = 0.0,
    maturity_redemption_price: float = 105.0,
    years_since_issued: int = 0,
) -> List[Tuple[float, float]]:
    """
    构建可转债现金流（使用实际剩余年限的分数时间点）

    对于 remaining_years=2.41 的债：
      第1次付息在 0.41年后，第2次在1.41年后，到期赎回在2.41年后

    Args:
        remaining_years: 剩余年限（可为小数）
        face_value: 面值
        annual_coupon_rates: 逐年票面利率列表
        maturity_redemption_price: 到期赎回价（含最后一期利息）
        years_since_issued: 已发行年数（用于阶梯利率定位）

    Returns:
        [(年份距离, 现金流金额), ...]
    """
    if remaining_years <= 0:
        return []

    cash_flows = []
    # 计算整数付息次数：如果剩余2.41年，则有3次现金流（0.41年、1.41年、2.41年）
    n_coupons = int(remaining_years) + 1  # 向上取整

    # 第一个时间点：小数部分
    frac = remaining_years - int(remaining_years)
    if frac == 0:
        frac = 1.0  # 整数年时，每年一次

    for i in range(n_coupons):
        # 时间点：i * 1 + frac（但最后一期可能恰好是整数）
        if i == n_coupons - 1:
            # 最后一期：到期赎回
            t = remaining_years  # 精确到到期日
        else:
            t = frac + i

        # 当年利率（基于已发行年数 + 当前付息周期）
        current_year = years_since_issued + i + 1
        if annual_coupon_rates and current_year <= len(annual_coupon_rates):
            rate = annual_coupon_rates[current_year - 1]
        elif annual_coupon_rates and len(annual_coupon_rates) > 0:
            rate = annual_coupon_rates[-1]
        else:
            rate = base_coupon_rate

        if i == n_coupons - 1:
            # 到期：赎回价（含最后一期利息）
            cash_flows.append((round(t, 4), maturity_redemption_price))
        else:
            # 中间：只付利息
            cash_flows.append((round(t, 4), face_value * rate / 100))

    return cash_flows


# ========== 收益率计算 ==========

def calc_ytm(price: float, cash_flows: List[Tuple[float, float]]) -> float:
    """
    计算税前到期收益率 (Yield to Maturity)
    通过求解 NPV=0 的折现率（IRR）

    Args:
        price: 当前转债价格
        cash_flows: [(年份距离, 现金流), ...]

    Returns:
        年化收益率（%），如 5.23 表示 5.23%
    """
    if price <= 0 or not cash_flows:
        return 0.0

    def npv(rate):
        total = 0.0
        for years, amount in cash_flows:
            total += amount / (1.0 + rate) ** years
        return total - price

    try:
        # brentq 需要两端异号
        # 利率很低时 NPV > 0（未来现金流折现 > 价格，说明价格便宜）
        # 利率很高时 NPV < 0
        ytm = brentq(npv, -0.99, 5.0, xtol=1e-6)
        return round(ytm * 100, 2)
    except ValueError:
        # 可能在某端同号，尝试简化计算
        return _calc_ytm_simple(price, cash_flows)
    except Exception as e:
        logger.warning(f"[CalcEngine] YTM 计算失败: {e}")
        return 0.0


def _calc_ytm_simple(price: float, cash_flows: List[Tuple[float, float]]) -> float:
    """
    简化版到期收益率计算（线性近似）
    YTM ≈ (到期总收益 - 当前价格) / 当前价格 / 年数
    """
    if price <= 0 or not cash_flows:
        return 0.0

    total_cash = sum(amt for _, amt in cash_flows)
    last_year = max(y for y, _ in cash_flows)

    if last_year <= 0:
        return 0.0

    return round(((total_cash - price) / price / last_year) * 100, 2)


def calc_put_yield(
    price: float,
    put_price: float,
    years_to_put: float,
    coupon_before_put: List[Tuple[float, float]],
) -> float:
    """
    计算税前回售收益率

    Args:
        price: 当前转债价格
        put_price: 回售价格（含当期利息）
        years_to_put: 距离回售期的年数
        coupon_before_put: 回售期前的利息现金流 [(年份, 金额), ...]

    Returns:
        年化回售收益率（%）
    """
    if price <= 0 or years_to_put <= 0:
        return 0.0

    # 现金流 = 回售期前利息 + 回售本金
    cash_flows = coupon_before_put + [(years_to_put, put_price)]

    def npv(rate):
        total = 0.0
        for years, amount in cash_flows:
            total += amount / (1.0 + rate) ** years
        return total - price

    try:
        ytm = brentq(npv, -0.99, 5.0, xtol=1e-6)
        return round(ytm * 100, 2)
    except ValueError:
        return 0.0
    except Exception:
        return 0.0


# ========== 纯债价值计算 ==========

def calc_pure_bond_value(
    cash_flows: List[Tuple[float, float]],
    discount_rate: float = 0.03,
) -> float:
    """
    纯债价值 = 未来现金流折现

    Args:
        cash_flows: [(年份, 金额), ...]
        discount_rate: 折现率（年化），默认3%近似同期限国债收益率

    Returns:
        纯债价值
    """
    if not cash_flows:
        return 0.0

    pv = 0.0
    for years, amount in cash_flows:
        pv += amount / (1.0 + discount_rate) ** years

    return round(pv, 2)


# ========== 期权价值 (BS 模型) ==========

def calc_option_value_bsm(
    stock_price: float,
    convert_price: float,
    volatility: float,
    risk_free_rate: float = 0.025,
    time_to_maturity: float = 1.0,
) -> float:
    """
    Black-Scholes 模型计算转债内含看涨期权价值

    Args:
        stock_price: 正股价格
        convert_price: 转股价 (行权价)
        volatility: 正股年化波动率 (小数，如0.25代表25%)
        risk_free_rate: 无风险利率 (默认2.5%)
        time_to_maturity: 剩余期限 (年)

    Returns:
        每张转债的期权价值 (面值100的转债)
    """
    if time_to_maturity <= 0 or volatility <= 0 or stock_price <= 0 or convert_price <= 0:
        return 0.0

    try:
        S = stock_price
        K = convert_price
        sigma = volatility
        r = risk_free_rate
        T = time_to_maturity

        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)

        # 看涨期权价值 (每股)
        call_per_share = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

        # 转债面值100，转股比例 = 100 / convert_price
        # 但期权价值已经是每股的，需要乘以转股比例
        # 转股比例 = 100 / convert_price
        convert_ratio = 100.0 / K
        option_value = call_per_share * convert_ratio

        return round(max(option_value, 0), 2)
    except Exception as e:
        logger.warning(f"[CalcEngine] BS模型计算失败: {e}")
        return 0.0


def calc_theoretical_value(
    pure_bond_value: float,
    option_value: float,
) -> float:
    """
    理论合理价值 = 纯债价值 + 期权价值
    """
    return round(pure_bond_value + option_value, 2)


def calc_deviation(
    bond_price: float,
    theoretical_value: float,
) -> float:
    """
    偏离度 = (市价 - 理论价值) / 理论价值 * 100
    """
    if theoretical_value <= 0:
        return 0.0
    return round((bond_price - theoretical_value) / theoretical_value * 100, 2)


# ========== 历史波动率 ==========

def calc_historical_volatility(
    daily_prices: List[float],
    periods_per_year: int = 252,
) -> float:
    """
    计算年化历史波动率

    Args:
        daily_prices: 正股日收盘价序列 (按日期升序)
        periods_per_year: 年交易日数 (A股252)

    Returns:
        年化波动率（%），如 25.3 代表 25.3%
    """
    if len(daily_prices) < 5:
        return 0.0

    # 计算日收益率
    returns = []
    for i in range(1, len(daily_prices)):
        if daily_prices[i - 1] > 0:
            r = np.log(daily_prices[i] / daily_prices[i - 1])
            returns.append(r)

    if len(returns) < 2:
        return 0.0

    # 年化波动率 = 日标准差 * sqrt(年交易日)
    daily_std = np.std(returns, ddof=1)
    annual_vol = daily_std * np.sqrt(periods_per_year)

    return round(annual_vol * 100, 2)
