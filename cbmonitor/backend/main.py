"""
CBMonitor - 可转债监控 FastAPI 主应用
参考 ArbDashboard/backend/main.py 的结构
"""
import os
import sys
import logging
from datetime import datetime
from typing import List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 确保 models 和 services 可以被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.database import db_manager, init_database, CBRecord
from models.schemas import BondVO, StrategyResultVO, DataStatusVO
from services.data_collector import collector
from services.strategy_engine import run_strategy, ALL_STRATEGIES

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("CBMonitor")

app = FastAPI(
    title="CBMonitor - 可转债监控",
    description="可转债数据采集、策略分析、投资决策支持",
    version="1.0.0"
)

# CORS - 允许前端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _record_to_vo(record: CBRecord) -> BondVO:
    """ORM 记录转 VO"""
    return BondVO(
        bond_code=record.bond_code or "",
        bond_name=record.bond_name or "",
        stock_code=record.stock_code or "",
        stock_name=record.stock_name or "",
        bond_price=record.bond_price or 0.0,
        bond_change_pct=record.bond_change_pct or 0.0,
        stock_price=record.stock_price or 0.0,
        stock_change_pct=record.stock_change_pct or 0.0,
        convert_price=record.convert_price or 0.0,
        convert_value=record.convert_value or 0.0,
        premium_rate=record.premium_rate or 0.0,
        double_low=record.double_low or 0.0,
        pre_tax_yield=record.pre_tax_yield or 0.0,
        pre_tax_put_yield=record.pre_tax_put_yield or 0.0,
        remaining_years=record.remaining_years or 0.0,
        remaining_size=record.remaining_size or 0.0,
        maturity_date=record.maturity_date or "",
        credit_rating=record.credit_rating or "",
        pure_bond_value=record.pure_bond_value or 0.0,
        put_value=record.put_value or 0.0,
        elasticity=record.elasticity or 0.0,
        pb_ratio=record.pb_ratio or 0.0,
        redeem_trigger_price=record.redeem_trigger_price or 0.0,
        put_trigger_price=record.put_trigger_price or 0.0,
        turnover_rate=record.turnover_rate or 0.0,
        turnover_amount=record.turnover_amount or 0.0,
        historical_volatility=record.historical_volatility or 0.0,
        ma20_premium_rate=record.ma20_premium_rate or 0.0,
        theoretical_value=record.theoretical_value or 0.0,
        deviation=record.deviation or 0.0,
        data_date=record.data_date or "",
        source=record.source or "",
    )


# ========== API 路由 ==========

@app.get("/")
async def root():
    return {"message": "CBMonitor API 运行中", "version": "1.0.0"}


@app.get("/api/status", response_model=DataStatusVO)
async def get_status():
    """获取数据采集状态"""
    status = db_manager.get_status()
    if not status:
        return DataStatusVO()
    return DataStatusVO(
        data_date=status.data_date or "",
        source=status.source or "",
        total_count=status.total_count or 0,
        updated_at=status.updated_at.strftime("%Y-%m-%d %H:%M:%S") if status.updated_at else ""
    )


@app.get("/api/bonds", response_model=List[BondVO])
async def get_bonds(
    date: str = Query("", description="数据日期，空=最新"),
    sort_by: str = Query("double_low", description="排序字段"),
    sort_order: str = Query("asc", description="排序方向 asc/desc"),
):
    """获取可转债列表"""
    if date:
        records = db_manager.get_snapshot_by_date(date)
    else:
        records = db_manager.get_latest_bonds()

    bonds = [_record_to_vo(r) for r in records]

    # 排序
    reverse = sort_order == "desc"
    if sort_by and hasattr(bonds[0] if bonds else None, sort_by):
        bonds.sort(key=lambda x: getattr(x, sort_by, 0) or 0, reverse=reverse)

    return bonds


@app.get("/api/strategy/{strategy_key}", response_model=StrategyResultVO)
async def get_strategy(
    strategy_key: str,
    n: int = Query(10, description="选取数量"),
    date: str = Query("", description="数据日期，空=最新"),
):
    """运行策略筛选"""
    if strategy_key not in ALL_STRATEGIES:
        raise HTTPException(status_code=404, detail=f"策略不存在: {strategy_key}")

    if date:
        records = db_manager.get_snapshot_by_date(date)
    else:
        records = db_manager.get_latest_bonds()

    if not records:
        raise HTTPException(status_code=404, detail="暂无数据，请先采集")

    bonds = [_record_to_vo(r) for r in records]
    return run_strategy(strategy_key, bonds, n)


@app.get("/api/strategies", response_model=List[str])
async def list_strategies():
    """列出所有可用策略"""
    return list(ALL_STRATEGIES.keys())


@app.post("/api/collect")
async def collect_data(source: str = Query("auto", description="数据源（保留兼容，实际使用AKShare+东方财富）")):
    """手动触发数据采集 - 基于 AKShare + 东方财富直连 + 自主计算引擎"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"[API] 开始采集数据, source={source}")
        raw_bonds = collector.collect(source)
        db_manager.save_daily_snapshot(today, raw_bonds, source)
        return {
            "success": True,
            "message": f"采集完成: {len(raw_bonds)} 条",
            "date": today,
            "source": source,
            "count": len(raw_bonds)
        }
    except Exception as e:
        logger.error(f"[API] 采集失败: {e}")
        raise HTTPException(status_code=500, detail=f"采集失败: {str(e)}")


# ========== 启动 ==========

if __name__ == "__main__":
    import uvicorn
    init_database()
    logger.info("=" * 50)
    logger.info("  CBMonitor - 可转债监控系统")
    logger.info("  API: http://127.0.0.1:8100")
    logger.info("  Docs: http://127.0.0.1:8100/docs")
    logger.info("=" * 50)
    try:
        # 只监听本机，公网经 nginx 反代进入（绝不听 0.0.0.0）
        uvicorn.run(app, host="127.0.0.1", port=8100)
    except KeyboardInterrupt:
        logger.info("服务器已停止")
