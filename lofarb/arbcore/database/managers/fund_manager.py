from .base import BaseManager
import sqlite3
import pandas as pd
from datetime import datetime
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class FundManager(BaseManager):
    # [AI-2026-07-25] 已删除 save_fund_data / update_fund_valuation（fund_data 表已废弃，零调用方死代码）

    def upsert_fund_factor(self, date: str, fund_code: str, calibration: float, hedge: float, position: float, nav: float = None):
        with self.lock:
            conn = self._get_conn()
            query = """
            INSERT OR REPLACE INTO fund_daily_factors (date, fund_code, calibration, hedge, position, nav, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, (datetime('now', 'localtime')))
            """
            conn.execute(query, (date, fund_code, calibration, hedge, position, nav))
            conn.commit()
            conn.close()
            
    def update_fund_pos_ratio(self, fund_code: str, pos_ratio: float):
        """更新 unified_fund_list 的 pos_ratio（Woody API 获取的最新仓位同步到静态配置）"""
        if pos_ratio is None:
            return
        with self.lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute('''
                    UPDATE unified_fund_list SET pos_ratio = ?
                    WHERE fund_code = ?
                ''', (pos_ratio, fund_code))
                if cursor.rowcount > 0:
                    logger.info(f"✅ pos_ratio 同步: {fund_code} → {pos_ratio*100:.2f}%")
                conn.commit()
            except Exception as e:
                logger.error(f"❌ 更新 {fund_code} pos_ratio 失败: {e}")
            finally:
                conn.close()
            
    def upsert_fund_basket_weight(self, date: str, fund_code: str, underlying_symbol: str, weight: float):
        with self.lock:
            conn = self._get_conn()
            query = """
            INSERT OR REPLACE INTO fund_basket_weights (date, fund_code, underlying_symbol, weight, updated_at)
            VALUES (?, ?, ?, ?, (datetime('now', 'localtime')))
            """
            conn.execute(query, (date, fund_code, underlying_symbol, weight))
            conn.commit()
            conn.close()

    def prune_fund_basket_weights(self, date: str, fund_code: str, valid_symbols: list):
        """[AI-2026-07-29] 权重换代清理：删除 (date, fund_code) 下不在新代符号集中的旧残留行。

        背景：woody 篮子参数每日可能"换代"（标的组合变化，如 160723 从
        USO/^USO-EU/^USO-JP 三标的换成 USO/^USO-EU 双标的）。
        旧逻辑只 INSERT OR REPLACE 新代符号，换代后消失的旧 symbol 行永远残留，
        导致该 (date, fund) 权重和 > 100%（实测 160723 2026-07-21 行 102.02%）。
        修复：每次同步某 (date, fund) 的权重前，先删除不在新代符号集的行。
        详见 docs_unfinished/权重换代残留bug修复说明_2026-07-29.md（含回滚方法）。
        """
        if not valid_symbols:
            return 0
        with self.lock:
            conn = self._get_conn()
            try:
                placeholders = ','.join('?' * len(valid_symbols))
                cur = conn.execute(
                    f"DELETE FROM fund_basket_weights WHERE date=? AND fund_code=? "
                    f"AND underlying_symbol NOT IN ({placeholders})",
                    (date, fund_code, *valid_symbols))
                deleted = cur.rowcount
                conn.commit()
                if deleted:
                    logger.info(f"🧹 权重换代清理: {fund_code}@{date} 删除旧代残留 {deleted} 行 (新代={valid_symbols})")
                return deleted
            finally:
                conn.close()

    def get_latest_fund_factor(self, fund_code: str):
        conn = self._get_conn()
        query = """
        SELECT date, calibration, hedge, position
        FROM fund_daily_factors 
        WHERE fund_code = ? 
        ORDER BY date DESC LIMIT 1
        """
        cursor = conn.execute(query, (fund_code,))
        result = cursor.fetchone()
        conn.close()
        if result:
            return {
                "date": result[0], "calibration": result[1], 
                "hedge": result[2], "position": result[3]
            }
        return None

    def get_fund_basket(self, date: str, fund_code: str):
        conn = self._get_conn()
        query = "SELECT underlying_symbol, weight FROM fund_basket_weights WHERE date = ? AND fund_code = ?"
        cursor = conn.execute(query, (date, fund_code))
        results = cursor.fetchall()
        conn.close()
        return [{"symbol": row[0], "weight": row[1]} for row in results]

    # [AI-2026-07-25] 已删除 get_latest_fund_price / batch_save_fund_prices / sync_jsl_fund_list / get_jsl_fund_list
    #               （fund_data、jsl_fund_list 表已废弃，零调用方死代码）

    def batch_save_fund_purchase_status(self, df):
        with self.lock:
            try:
                conn = self._get_conn()
                records = df.to_records(index=False)
                conn.executemany('''
                    INSERT OR REPLACE INTO fund_purchase_status 
                    (fund_code, purchase_status, redemption_status, purchase_fee, redemption_fee, purchase_limit, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, (datetime('now', 'localtime')))
                ''', records)
                conn.commit()
                logger.info(f"Successfully cached {len(df)} fund purchase status items!")
            except Exception as e:
                logger.error(f"Failed to batch save fund purchase status: {e}")
            finally:
                conn.close()

    def get_fund_purchase_status(self, fund_code: str) -> Dict[str, str]:
        conn = self._get_conn()
        cursor = conn.execute('''
            SELECT purchase_status, redemption_status, purchase_fee, redemption_fee
            FROM fund_purchase_status WHERE fund_code = ?
        ''', (fund_code,))
        r = cursor.fetchone()
        conn.close()
        if r:
            return {
                'purchase_status': r[0], 'redemption_status': r[1],
                'purchase_fee': r[2], 'redemption_fee': r[3]
            }
        return {
            'purchase_status': '未知', 'redemption_status': '未知',
            'purchase_fee': '0%', 'redemption_fee': '0.50%'
        }

    def sync_unified_fund_list(self, fund_list: List[Dict[str, Any]]):
        """[AI-2026-10-11] DB 权威化：YAML 降级为「种子 / 备用快照」。

        旧实现有两个与「一切以 DB 为准」相悖的动作，本方法已改掉：
          1) `ON CONFLICT DO UPDATE` —— 会用 YAML 覆盖 DB 已有行
             （后果：DB 里改过的 category/related_index 会被 YAML 还原）；
          2) `DELETE ... WHERE fund_code NOT IN (yaml_codes)` —— 会删除 DB 里
             不在 YAML 的行（后果：只写 DB 的基金被清掉）。
        而 main.py lifespan（`sys.platform != 'win32'` 分支）在 ARM 启动时会调用本方法
        ⇒ 上述两动作必被触发。故改为「只补缺、不覆盖、不删除」。

        新语义：仅把 YAML 中 DB 尚不存在的基金 INSERT 进来（种子），
        已存在的行一律保持 DB 原值；删基金须显式操作 DB。
        主键 = fund_code。
        """
        with self.lock:
            try:
                conn = self._get_conn()
                seeded = 0
                for item in fund_list:
                    cur = conn.execute('''
                        INSERT INTO unified_fund_list
                        (category, fund_code, fund_name, related_index, idx_code, idx_name, pos_ratio, target_type, data_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fund_code) DO NOTHING
                    ''', (
                        item['category'],
                        item['code'],
                        item['name'],
                        item.get('related_index', '-'),
                        item.get('idx_code', item.get('related_index', '-')),
                        item.get('idx_name', '-'),
                        item.get('pos_ratio', 0.95),
                        item.get('target_type', 'ETF'),
                        item.get('data_source', '') or ''
                    ))
                    seeded += cur.rowcount
                conn.commit()
                logger.info(f"[DB权威] unified_fund_list 种子同步：YAML {len(fund_list)} 只，新补 {seeded} 只（不覆盖/不删除）")
            except Exception as e:
                logger.error(f"Failed to sync unified fund list: {e}")
            finally:
                conn.close()

    def get_unified_fund_list(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.execute("SELECT category, fund_code, fund_name, related_index, pos_ratio, target_type, data_source FROM unified_fund_list")
        results = [{"category": r[0], "code": r[1], "name": r[2], "related_index": r[3], "pos_ratio": r[4], "target_type": r[5], "data_source": r[6]} for r in cursor.fetchall()]
        conn.close()
        return results

    def save_unified_history(self, date_str, fund_code, **kwargs):
        """
        [V3.0] 极简通用型历史数据保存器
        支持动态列更新，自动处理 NULL 覆盖问题。
        [AI-2026-10-09] 打 writer 标识（默认 'lofarb:fund_manager'），供事后追查"这行是谁写的"
        （凌晨批来源排查教训）。调用方可用 kwargs['writer'] 覆盖为更具体的来源。
        """
        with self.lock:
            conn = self._get_conn()
            try:
                # 过滤掉 None 值，避免覆盖已有数据
                valid_data = {k: v for k, v in kwargs.items() if v is not None}
                if 'writer' not in valid_data:
                    valid_data['writer'] = 'lofarb:fund_manager'
                if not valid_data: return

                cols = ['date', 'fund_code'] + list(valid_data.keys())
                placeholders = ['?'] * len(cols)
                vals = [date_str, fund_code] + list(valid_data.values())

                update_clause = ", ".join([f"{k} = excluded.{k}" for k in valid_data.keys()])

                query = f"""
                    INSERT INTO unified_fund_history ({", ".join(cols)})
                    VALUES ({", ".join(placeholders)})
                    ON CONFLICT(date, fund_code) DO UPDATE SET
                    {update_clause}
                """
                conn.execute(query, vals)
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to save unified history for {fund_code}: {e}")
            finally:
                conn.close()

