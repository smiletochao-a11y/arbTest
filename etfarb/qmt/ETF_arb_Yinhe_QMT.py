# ETFarb QMT 策略（简化版）
# 职责：仅接收信号，执行下单
# 数据由后端服务提供，本策略不采集数据

#encoding:gbk
"""
ETF 申赎套利 QMT 策略（简化版）
文件名：ETF_arb_Yinhe_QMT.py
版本：1.6.0 (2026-08-30)
变更日志：
  v1.6.0 (2026-08-30) - 剥离数据采集和计算功能，仅保留下单逻辑
  v1.5.5 (2026-08-29) - 新增 GPCSP006 静态估值（已移除）
  v1.5.4 (2026-08-29) - 新增费用拆解列
"""

import builtins
import json
import os
import time
from typing import Dict, Optional

# ==================== 配置 ====================
STRATEGY_VERSION = '1.6.0 (2026-08-30)'

# 后端服务地址
BACKEND_URL = 'http://127.0.0.1:8101'

# 监控标的（与后端保持一致）
WATCH = [
    ("510300.SH", "沪深300ETF", "国内宽基", 30, -30),
    ("159687.SZ", "亚太精选ETF南方", "跨境QDII", 50, -50),
]

# 阈值
THRESHOLD_PREMIUM = 30
THRESHOLD_DISCOUNT = -30

# 快照路径（后端写入）
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "etf_snapshot.json")


# ==================== 共享状态 ====================
_ETF_KEY = '_etfarb_state'

def _S():
    s = getattr(builtins, ETF_KEY, None)
    if s is None:
        s = {}
        setattr(builtins, ETF_KEY, s)
    return s


# ==================== 数据获取 ====================

def _fetch_snapshot() -> Optional[Dict]:
    """从后端获取快照数据"""
    try:
        import urllib.request
        req = urllib.request.Request(BACKEND_URL + '/api/snapshot')
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"[QMT] 获取快照失败: {e}")
        return None


# ==================== 信号处理 ====================

def _check_signal(etf):
    """检查套利信号"""
    code = etf['code']
    premium = etf.get('premium_bps')
    
    if premium is None:
        return None
    
    # 溢价信号
    if premium >= THRESHOLD_PREMIUM:
        return {
            'code': code,
            'name': etf.get('name', ''),
            'type': 'premium',
            'premium_bps': premium,
            'action': '申购ETF/卖出成分股',
        }
    
    # 折价信号
    if premium <= THRESHOLD_DISCOUNT:
        return {
            'code': code,
            'name': etf.get('name', ''),
            'type': 'discount',
            'premium_bps': premium,
            'action': '赎回ETF/买入成分股',
        }
    
    return None


# ==================== 主循环 ====================

def scan_once():
    """扫描一次"""
    st = _S()
    
    # 获取快照
    snapshot = _fetch_snapshot()
    if not snapshot or 'etfs' not in snapshot:
        return
    
    for etf in snapshot.get('etfs', []):
        signal = _check_signal(etf)
        if signal:
            print(f"\n[{time.strftime('%H:%M:%S')}] *** 套利信号 {signal['code']} ***")
            print(f"    类型: {signal['type']}")
            print(f"    溢价: {signal['premium_bps']:+.1f} bp")
            print(f"    建议: {signal['action']}")
            print(f"    >>> 请人工确认后再下单")
            
            # 记录日志
            logs = st.get('logs', [])
            logs.append({
                't': time.strftime('%H:%M:%S'),
                'level': 'SIGNAL',
                'msg': f"{signal['code']} {signal['type']} {signal['premium_bps']:+.1f}bp"
            })
            st['logs'] = logs[-50:]


def handlebar(ContextInfo):
    """QMT 框架要求"""
    time.sleep(0.001)


def on_3s(ContextInfo):
    """每 3 秒执行一次"""
    scan_once()


def init(ContextInfo):
    """初始化"""
    print(f"\n{'='*50}")
    print(f"ETFarb QMT 策略 v{STRATEGY_VERSION}")
    print(f"{'='*50}")
    print(f"后端服务: {BACKEND_URL}")
    print(f"监控标的: {[w[0] for w in WATCH]}")
    print(f"{'='*50}\n")
    
    # 初始扫描
    scan_once()


def stop(ContextInfo):
    """停止"""
    print("\n[QMT] 策略已停止")
