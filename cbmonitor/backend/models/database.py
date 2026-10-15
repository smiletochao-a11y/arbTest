"""
可转债数据模型 - SQLAlchemy ORM
参考 arbTest/arbcore/database/db_manager.py 的设计模式
"""
import os
from datetime import datetime
from typing import List, Optional
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager

# ---- 程序根定位（双端兼容，详见 <ARB>/README.md §2）----
#   本地: src/backend/models/database.py → 含 "src" 的那层父目录的父 = 程序根
#   ARM : /home/ubuntu/cbmonitor/backend/models/... → 上溯 2 层即程序根（扁平布局，无 src 层）
# ⚠️ 绝不用 dirname(dirname(__file__)) 当程序根：本地会算成 src/backend，DB 落进 src/ 内。
_MODELS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_MODELS_DIR)
_PARENT = os.path.dirname(_BACKEND_DIR)                      # 本地 = .../CBMonitor/src，ARM = 程序根
PROJ_ROOT = os.path.dirname(_PARENT) if os.path.basename(_PARENT) == "src" else _PARENT

# ---- 数据库目录与 src/ 平级（2026-10-07 搬迁）----
# 🔴 必须与 src/ 物理隔离：db 落在 src/ 内会进 git 仓库（有被 push 的风险），
#    且按部署约定推 src/ 上 ARM 时会覆盖生产库。
DB_DIR = os.path.join(PROJ_ROOT, "database")
DB_PATH = os.path.join(DB_DIR, "cb_monitor.db")
os.makedirs(DB_DIR, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    echo=False
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class CBRecord(Base):
    """可转债每日快照表"""
    __tablename__ = "cb_daily_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_date = Column(String(10), nullable=False, index=True)
    bond_code = Column(String(10), nullable=False, index=True)
    bond_name = Column(String(50), nullable=False)
    stock_code = Column(String(10), default="")
    stock_name = Column(String(50), default="")

    bond_price = Column(Float, default=0.0)
    bond_change_pct = Column(Float, default=0.0)
    stock_price = Column(Float, default=0.0)
    stock_change_pct = Column(Float, default=0.0)

    convert_price = Column(Float, default=0.0)
    convert_value = Column(Float, default=0.0)
    premium_rate = Column(Float, default=0.0)

    double_low = Column(Float, default=0.0)
    pre_tax_yield = Column(Float, default=0.0)
    pre_tax_put_yield = Column(Float, default=0.0)

    remaining_years = Column(Float, default=0.0)
    remaining_size = Column(Float, default=0.0)
    maturity_date = Column(String(10), default="")

    credit_rating = Column(String(10), default="")

    pure_bond_value = Column(Float, default=0.0)
    put_value = Column(Float, default=0.0)
    elasticity = Column(Float, default=0.0)
    pb_ratio = Column(Float, default=0.0)

    redeem_trigger_price = Column(Float, default=0.0)
    put_trigger_price = Column(Float, default=0.0)

    turnover_rate = Column(Float, default=0.0)
    turnover_amount = Column(Float, default=0.0)

    historical_volatility = Column(Float, default=0.0)
    ma20_premium_rate = Column(Float, default=0.0)
    theoretical_value = Column(Float, default=0.0)
    deviation = Column(Float, default=0.0)

    source = Column(String(20), default="ninwin")
    created_at = Column(DateTime, default=datetime.now)


class DataStatusRecord(Base):
    """数据采集状态表"""
    __tablename__ = "data_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_date = Column(String(10), nullable=False, unique=True)
    source = Column(String(20), default="")
    total_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now)


def init_database():
    Base.metadata.create_all(bind=engine)
    print(f"[DB] 数据库初始化完成: {DB_PATH}")


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CBDatabaseManager:
    """可转债数据库管理器 - 参考 arbcore.database.db_manager.DatabaseManager"""

    def save_daily_snapshot(self, data_date: str, bonds: List[dict], source: str = "ninwin"):
        with get_db() as db:
            db.query(CBRecord).filter(CBRecord.data_date == data_date).delete()
            for b in bonds:
                cols = CBRecord.__table__.columns.keys()
                record = CBRecord(
                    data_date=data_date, source=source,
                    **{k: v for k, v in b.items() if k in cols and k not in ('id', 'created_at', 'data_date', 'source')}
                )
                db.add(record)

            status = db.query(DataStatusRecord).filter(DataStatusRecord.data_date == data_date).first()
            if status:
                status.source = source
                status.total_count = len(bonds)
                status.updated_at = datetime.now()
            else:
                db.add(DataStatusRecord(data_date=data_date, source=source, total_count=len(bonds)))
            db.commit()
            print(f"[DB] 保存 {data_date} 快照: {len(bonds)} 条")

    def get_latest_bonds(self) -> List[CBRecord]:
        with get_db() as db:
            latest = db.query(CBRecord.data_date).order_by(CBRecord.data_date.desc()).first()
            if not latest:
                return []
            return db.query(CBRecord).filter(CBRecord.data_date == latest[0]).all()

    def get_snapshot_by_date(self, data_date: str) -> List[CBRecord]:
        """获取指定日期的数据快照"""
        with get_db() as db:
            return db.query(CBRecord).filter(
                CBRecord.data_date == data_date
            ).all()

    def get_status(self) -> Optional[DataStatusRecord]:
        with get_db() as db:
            return db.query(DataStatusRecord).order_by(DataStatusRecord.data_date.desc()).first()


db_manager = CBDatabaseManager()
