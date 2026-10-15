"""
可转债数据采集服务 - 基于 AKShare + 东方财富直连 + 新浪财经 + 自主计算
完全独立自主，不依赖集思录/宁稳网

数据链路：
  交易所 → 东方财富(AKShare)/新浪财经 → 本模块 → 自主计算衍生指标

数据来源：
1. AKShare bond_zh_cov() → 东方财富一览表（主数据源：价格/溢价/正股/规模/评级）
2. AKShare bond_zh_hs_cov_spot() → 新浪财经实时行情（涨跌幅/成交量/成交额）
3. 东方财富数据中心直连 API → 转债条款（到期日/阶梯利率/评级/条款文本）
4. 自主计算引擎(calc_engine) → 双低/收益率/弹性/触发价/纯债价值
"""
import os
import sys
import re
import logging
from datetime import datetime
from typing import List, Dict, Optional
import requests
import pandas as pd

# ===== NO_PROXY: 屏蔽系统代理 =====
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
# 屏蔽 AKShare 内部 tqdm 进度条（那个 67% 的进度条）
os.environ['TQDM_DISABLE'] = '1'

# 屏蔽 pandas FutureWarning（AKShare 内部 concat 触发的）
import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.calc_engine import (
    calc_double_low,
    calc_elasticity,
    calc_remaining_years,
    calc_pure_bond_value,
    build_cash_flows,
    calc_ytm,
)

logger = logging.getLogger(__name__)
SESSION_PROXIES = {"http": None, "https": None}


# ========== 东方财富数据中心直连 API ==========

class EastMoneyDataCenter:
    """东方财富数据中心 - 获取转债条款"""

    BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.proxies = SESSION_PROXIES
        self.session.trust_env = False

    def fetch_bond_terms(self) -> Dict[str, Dict]:
        result = {}
        page = 1
        while True:
            params = {
                "reportName": "RPT_BOND_CB_LIST",
                "columns": "ALL",
                "pageSize": "100",
                "pageNumber": str(page),
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "source": "WEB",
                "client": "WEB",
            }
            try:
                resp = self.session.get(self.BASE_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                rows = data.get("result", {}).get("data", []) if data.get("result") else []
                if not rows:
                    break
                for item in rows:
                    code = str(item.get("SECURITY_CODE", ""))
                    if not code:
                        continue
                    exec_price_sh = self._safe_float(item.get("EXECUTE_PRICE_SH"))
                    exec_price_hs = self._safe_float(item.get("EXECUTE_PRICE_HS"))
                    maturity_redeem = exec_price_sh if exec_price_sh > 0 else exec_price_hs
                    result[code] = {
                        "maturity_date": str(item.get("EXPIRE_DATE", "") or ""),
                        "delist_date": str(item.get("DELIST_DATE", "") or ""),
                        "interest_rate_explain": str(item.get("INTEREST_RATE_EXPLAIN", "") or ""),
                        "issue_size": self._safe_float(item.get("ACTUAL_ISSUE_SCALE")),
                        "rating": str(item.get("RATING", "") or ""),
                        "maturity_redeem_price": maturity_redeem,
                        "value_date": str(item.get("VALUE_DATE", "") or ""),
                        "bond_expire": self._safe_float(item.get("BOND_EXPIRE")),
                        "par_value": self._safe_float(item.get("PAR_VALUE"), 100.0),
                    }
                if len(rows) < 100:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"[EM_DataCenter] 获取条款失败 (page {page}): {e}")
                break
        logger.info(f"[EM_DataCenter] 获取到 {len(result)} 条条款信息")
        return result

    @staticmethod
    def _safe_float(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (ValueError, TypeError):
            return default


# ========== 工具函数 ==========

def parse_coupon_rates(explain: str) -> List[float]:
    """
    解析阶梯利率说明文本
    输入: "第一年0.20%,第二年0.40%,第三年0.60%,第四年1.00%,第五年1.50%,第六年2.00%"
    输出: [0.20, 0.40, 0.60, 1.00, 1.50, 2.00]
    """
    if not explain:
        return []
    # 匹配 "第X年Y.YY%" 模式
    matches = re.findall(r'第[一二三四五六七八九十\d]+年\s*([\d.]+)%', explain)
    if matches:
        return [float(m) for m in matches]
    # 兜底：匹配所有百分比
    matches = re.findall(r'([\d.]+)%', explain)
    return [float(m) for m in matches] if matches else []


# ========== AKShare 采集器 ==========

class AKShareCollector:
    """
    数据采集器
    主数据源：bond_zh_cov() 一览表
    实时补充：bond_zh_hs_cov_spot() 新浪财经
    条款补充：东方财富数据中心直连 API
    衍生指标：自主计算
    """

    def __init__(self):
        self.em_api = EastMoneyDataCenter()

    def fetch_cb_list(self) -> List[Dict]:
        logger.info("=" * 50)
        logger.info("[Collector] 开始采集可转债数据")

        # Step 1: 一览表（主数据源）
        df_basic = self._fetch_basic()

        # Step 2: 新浪实时行情（涨跌幅/成交额）
        df_spot = self._fetch_sina_spot()

        # Step 3: 条款信息
        bond_terms = self.em_api.fetch_bond_terms()

        # Step 4: 合并 + 自主计算
        bonds = self._merge_and_calculate(df_basic, df_spot, bond_terms)

        logger.info(f"[Collector] 采集完成: {len(bonds)} 条可转债数据")
        logger.info("=" * 50)
        return bonds

    def _fetch_basic(self) -> pd.DataFrame:
        try:
            import akshare as ak
            df = ak.bond_zh_cov()
            logger.info(f"[AKShare] 一览表: {len(df)} 条")
            return df
        except Exception as e:
            logger.error(f"[AKShare] 一览表获取失败: {e}")
            return pd.DataFrame()

    def _fetch_sina_spot(self) -> pd.DataFrame:
        """获取新浪财经实时行情"""
        try:
            import akshare as ak
            df = ak.bond_zh_hs_cov_spot()
            logger.info(f"[Sina] 实时行情: {len(df)} 条, 列: {list(df.columns)}")
            return df
        except Exception as e:
            logger.warning(f"[Sina] 实时行情获取失败: {e}")
            return pd.DataFrame()

    def _merge_and_calculate(
        self,
        df_basic: pd.DataFrame,
        df_spot: pd.DataFrame,
        bond_terms: Dict[str, Dict],
    ) -> List[Dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        bonds = []

        if df_basic.empty:
            logger.error("[Collector] 一览表为空")
            return bonds

        logger.info(f"[Collector] 处理 {len(df_basic)} 条")

        skipped = 0
        for _, row in df_basic.iterrows():
            bond = self._parse_basic_row(row, today)
            if not bond["bond_code"] or bond["bond_price"] <= 0:
                continue

            # 补充条款信息
            terms = bond_terms.get(bond["bond_code"])
            if terms:
                self._enrich_from_terms(bond, terms)

            # 过滤已退市/到期
            if not self._is_active_bond(bond, terms):
                skipped += 1
                continue

            # 补充新浪实时数据
            if not df_spot.empty:
                self._enrich_from_sina(bond, df_spot)

            # 计算触发价（如果数据中心没有）
            self._calc_trigger_prices(bond)

            # 计算到期赎回价（如果数据中心没有）
            self._calc_maturity_redeem_price(bond, terms)

            # 计算回售价值
            self._calc_put_value(bond)

            # ===== 自主计算衍生指标 =====
            self._calculate_derived_metrics(bond, terms)

            bonds.append(bond)

        if skipped:
            logger.info(f"[Collector] 过滤掉 {skipped} 条已退市/到期转债")

        # 批量获取正股涨跌幅
        self._enrich_stock_change(bonds)

        return bonds

    def _parse_basic_row(self, row, today: str) -> Dict:
        def gf(col, default=0.0):
            v = row.get(col, default)
            try:
                f = float(v)
                return f if not pd.isna(f) else default
            except (ValueError, TypeError):
                return default

        def gs(col, default=""):
            v = row.get(col, default)
            if v is None:
                return default
            try:
                if pd.isna(v):
                    return default
            except (TypeError, ValueError):
                pass
            return str(v).strip()

        return {
            "data_date": today,
            "source": "akshare_sina_em",
            "bond_code": gs("债券代码"),
            "bond_name": gs("债券简称"),
            "stock_code": gs("正股代码"),
            "stock_name": gs("正股简称"),
            "bond_price": gf("债现价"),
            "bond_change_pct": 0.0,
            "stock_price": gf("正股价"),
            "stock_change_pct": 0.0,
            "convert_price": gf("转股价"),
            "convert_value": gf("转股价值"),
            "premium_rate": gf("转股溢价率"),
            "remaining_size": gf("发行规模"),
            "remaining_years": 0.0,
            "maturity_date": "",
            "list_date": gs("上市时间"),
            "credit_rating": gs("信用评级"),
            "pure_bond_value": 0.0,
            "put_trigger_price": 0.0,
            "redeem_trigger_price": 0.0,
            "maturity_redeem_price": 0.0,
            "put_value": 0.0,
            "double_low": 0.0,
            "pre_tax_yield": 0.0,
            "pre_tax_put_yield": 0.0,
            "elasticity": 0.0,
            "pb_ratio": 0.0,
            "turnover_rate": 0.0,
            "turnover_amount": 0.0,
            "historical_volatility": 0.0,
            "ma20_premium_rate": 0.0,
            "theoretical_value": 0.0,
            "deviation": 0.0,
        }

    def _enrich_from_sina(self, bond: Dict, df_spot: pd.DataFrame):
        """从新浪实时行情补充：涨跌幅、成交额、换手率"""
        code = bond["bond_code"]
        if "code" not in df_spot.columns:
            return
        matches = df_spot[df_spot["code"].astype(str) == code]
        if matches.empty:
            return
        row = matches.iloc[0]

        try:
            bond["bond_change_pct"] = float(row.get("changepercent", 0) or 0)
        except (ValueError, TypeError):
            pass

        try:
            amount = float(row.get("amount", 0) or 0)
            bond["turnover_amount"] = round(amount / 10000, 2)  # 转为万元
        except (ValueError, TypeError):
            pass

        # 换手率 = 成交量 / 剩余规模对应的张数 * 100
        try:
            volume = float(row.get("volume", 0) or 0)
            if bond["remaining_size"] > 0:
                total_bonds = bond["remaining_size"] * 1000000  # 亿→万张
                if total_bonds > 0:
                    bond["turnover_rate"] = round(volume / total_bonds * 100, 2)
        except (ValueError, TypeError):
            pass

    def _enrich_stock_change(self, bonds: List[Dict]):
        """批量获取正股涨跌幅（新浪财经 API）"""
        if not bonds:
            return

        # 收集所有正股代码并添加市场前缀
        stock_codes = {}
        for b in bonds:
            code = b.get("stock_code", "").strip()
            if not code:
                continue
            prefix = self._stock_market_prefix(code)
            if prefix:
                stock_codes[code] = f"{prefix}{code}"

        if not stock_codes:
            return

        logger.info(f"[Sina] 批量获取 {len(stock_codes)} 只正股行情")

        # 新浪 API 每 URL 最多约 80 只，分批请求
        sina_codes = list(stock_codes.values())
        batch_size = 60
        stock_data = {}

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn/",
        })
        session.proxies = SESSION_PROXIES
        session.trust_env = False

        for i in range(0, len(sina_codes), batch_size):
            batch = sina_codes[i:i + batch_size]
            url = f"http://hq.sinajs.cn/list={','.join(batch)}"
            try:
                resp = session.get(url, timeout=15)
                resp.encoding = "gbk"
                for line in resp.text.strip().split("\n"):
                    m = re.match(r'var hq_str_(\w+)="(.+)"', line.strip())
                    if m:
                        full_code = m.group(1)
                        fields = m.group(2).split(",")
                        if len(fields) >= 4:
                            prev_close = float(fields[2]) if fields[2] else 0
                            current = float(fields[3]) if fields[3] else 0
                            if prev_close > 0:
                                change_pct = round((current - prev_close) / prev_close * 100, 2)
                                # 提取纯代码（去掉 sh/sz/bj 前缀）
                                pure_code = full_code[2:] if full_code[:2] in ("sh", "sz", "bj") else full_code
                                stock_data[pure_code] = change_pct
            except Exception as e:
                logger.warning(f"[Sina] 正股行情批次 {i//batch_size} 获取失败: {e}")

        # 填充到 bonds
        filled = 0
        for b in bonds:
            code = b.get("stock_code", "").strip()
            if code in stock_data:
                b["stock_change_pct"] = stock_data[code]
                filled += 1

        logger.info(f"[Sina] 正股涨跌幅填充: {filled}/{len(bonds)}")

    @staticmethod
    def _stock_market_prefix(code: str) -> str:
        """根据股票代码确定市场前缀"""
        if not code:
            return ""
        if code.startswith("6"):
            return "sh"
        elif code.startswith(("0", "3")):
            return "sz"
        elif code.startswith(("8", "4")):
            return "bj"
        return ""

    def _enrich_from_terms(self, bond: Dict, terms: Dict):
        """从东方财富条款数据补充"""
        if not bond["maturity_date"]:
            bond["maturity_date"] = terms.get("maturity_date", "")
        if not bond["credit_rating"]:
            bond["credit_rating"] = terms.get("rating", "")
        if bond["remaining_size"] == 0:
            bond["remaining_size"] = terms.get("issue_size", 0.0)
        if bond["maturity_redeem_price"] == 0:
            bond["maturity_redeem_price"] = terms.get("maturity_redeem_price", 0.0)

    def _calc_trigger_prices(self, bond: Dict):
        """计算触发价（数据中心对活跃转债没有，自己算）"""
        cp = bond["convert_price"]
        if cp <= 0:
            return
        # 强赎触发价 ≈ 转股价 * 1.3（标准条款，少数转债用1.2或1.25）
        if bond["redeem_trigger_price"] == 0:
            bond["redeem_trigger_price"] = round(cp * 1.3, 2)
        # 回售触发价 ≈ 转股价 * 0.7（标准条款）
        if bond["put_trigger_price"] == 0:
            bond["put_trigger_price"] = round(cp * 0.7, 2)

    def _calc_maturity_redeem_price(self, bond: Dict, terms: Optional[Dict]):
        """计算到期赎回价（数据中心对活跃转债没有，从利率说明推算）"""
        if bond["maturity_redeem_price"] > 0:
            return  # 已有值

        if not terms:
            bond["maturity_redeem_price"] = 105.0  # 常见默认值
            return

        explain = terms.get("interest_rate_explain", "")
        rates = parse_coupon_rates(explain)
        par = terms.get("par_value", 100.0)

        if rates:
            # 到期赎回价 = 面值 + 最后一期利息（最低估算，实际可能还有补偿利息）
            last_rate = rates[-1]
            bond["maturity_redeem_price"] = round(par + par * last_rate / 100, 2)
        else:
            bond["maturity_redeem_price"] = 105.0

    def _calc_put_value(self, bond: Dict):
        """计算回售价值（通常=面值+当期利息，暂用100作为保守估计）"""
        if bond["put_value"] == 0:
            bond["put_value"] = 100.0  # 面值，保守估计

    def _is_active_bond(self, bond: Dict, terms: Optional[Dict] = None) -> bool:
        name = bond.get("bond_name", "")
        code = bond.get("bond_code", "")
        if "退" in name:
            return False
        if code.startswith("40"):
            return False
        # 未上市：上市时间为空或在未来
        list_date = bond.get("list_date", "")
        if not list_date:
            return False
        try:
            from datetime import datetime as _dt
            ld = _dt.strptime(list_date[:10], "%Y-%m-%d")
            if ld > _dt.now():
                return False
        except (ValueError, TypeError):
            pass
        # 已到期
        maturity = bond.get("maturity_date", "")
        if maturity and calc_remaining_years(maturity) <= 0:
            return False
        # 已退市
        if terms and terms.get("delist_date"):
            return False
        return True

    @staticmethod
    def _gf(row, col, default=0.0):
        v = row.get(col, default)
        try:
            f = float(v)
            return f if not pd.isna(f) else default
        except (ValueError, TypeError):
            return default

    def _calculate_derived_metrics(self, bond: Dict, terms: Optional[Dict] = None):
        """自主计算所有衍生指标"""

        # 1. 双低值
        bond["double_low"] = calc_double_low(bond["bond_price"], bond["premium_rate"])

        # 2. 弹性
        bond["elasticity"] = calc_elasticity(bond["convert_value"], bond["bond_price"])

        # 3. 剩余年限
        if bond["maturity_date"]:
            bond["remaining_years"] = calc_remaining_years(bond["maturity_date"])

        # 4. 税前到期收益率（用阶梯利率构建现金流 → IRR）
        if bond["remaining_years"] > 0 and bond["maturity_redeem_price"] > 0:
            price = bond["bond_price"]
            redeem = bond["maturity_redeem_price"]
            years = bond["remaining_years"]

            if terms and terms.get("interest_rate_explain"):
                # 有阶梯利率：构建完整现金流 → IRR
                rates = parse_coupon_rates(terms["interest_rate_explain"])
                par = terms.get("par_value", 100.0)
                bond_expire = terms.get("bond_expire", len(rates))
                value_date = terms.get("value_date", "")

                # 计算已发行年数
                years_issued = 0
                if value_date:
                    try:
                        vd = datetime.strptime(value_date[:10], "%Y-%m-%d")
                        years_issued = (datetime.now() - vd).days / 365.25
                    except:
                        pass

                cash_flows = build_cash_flows(
                    remaining_years=years,
                    face_value=par,
                    annual_coupon_rates=rates,
                    maturity_redemption_price=redeem,
                    years_since_issued=int(years_issued),
                )

                if cash_flows:
                    ytm = calc_ytm(price, cash_flows)
                    if ytm > 0:
                        bond["pre_tax_yield"] = ytm
                    else:
                        # IRR失败，用简化公式
                        bond["pre_tax_yield"] = round(
                            ((redeem - price) / price / years) * 100, 2
                        )
                else:
                    bond["pre_tax_yield"] = round(
                        ((redeem - price) / price / years) * 100, 2
                    )
            else:
                # 无阶梯利率：简化公式
                if price > 0 and years > 0:
                    bond["pre_tax_yield"] = round(
                        ((redeem - price) / price / years) * 100, 2
                    )

        # 5. 税前回售收益率
        if bond["remaining_years"] > 0 and bond["put_value"] > 0:
            price = bond["bond_price"]
            put_price = bond["put_value"]
            years = bond["remaining_years"]
            if price > 0 and years > 0:
                bond["pre_tax_put_yield"] = round(
                    ((put_price - price) / price / years) * 100, 2
                )

        # 6. 纯债价值（自主计算）
        if bond["pure_bond_value"] == 0 and bond["remaining_years"] > 0:
            redeem = bond["maturity_redeem_price"]
            if redeem > 0:
                cash_flows = [(bond["remaining_years"], redeem)]
                bond["pure_bond_value"] = calc_pure_bond_value(cash_flows, discount_rate=0.03)


# ========== 采集器入口 ==========

class DataCollectorService:
    def __init__(self):
        self.collector = AKShareCollector()

    def collect(self, source: str = "auto") -> List[Dict]:
        return self.collector.fetch_cb_list()


collector = DataCollectorService()
