import os
import sys
import time
import socket
import threading

# Ensure LOFarb directory is in sys.path so we can find account_private.py when imported from elsewhere
_tm_dir = os.path.dirname(os.path.abspath(__file__))
_lof_dir = os.path.normpath(os.path.join(_tm_dir, "..", "..", "LOFarb"))
if os.path.exists(_lof_dir) and _lof_dir not in sys.path:
    sys.path.append(_lof_dir)

import logging
logger = logging.getLogger(__name__)

# 优先尝试从 arbcore.config 导入
try:
    from arbcore.config.account_private import GJS_ACCOUNT
except ImportError:
    try:
        # 兼容旧路径
        from account_private import GJS_ACCOUNT
    except ImportError:
        print("WARNING: account_private.py 不存在，请复制 account_example.py 并填入真实账号")
        GJS_ACCOUNT = None

class TradeManager:
    """A股/LOF统一交易接口管理器"""
    def __init__(self):
        self.tdx_available = False
        self.tq = None
        self.tqconst = None
        self.tdx_account_id = None
        
        self.xtquant_available = False
        self.xt_trader = None
        self.xt_account = None
        self.xtconstant = None

        # 启动时自动初始化可用通道
        self._init_tdx()
        # [V9.1] 国金QMT初始化放后台线程，不阻塞 uvicorn 启动
        threading.Thread(target=self._init_guojin_qmt, daemon=True).start()

    def _init_tdx(self):
        try:
            # 仅使用新版 tqcenter 路径
            tdx_api_path = r'D:\new_tdx_test\PYPlugins\user'
            
            # 清除旧版缓存
            if r'D:\new_tdx64\PYPlugins\user' in sys.path:
                sys.path.remove(r'D:\new_tdx64\PYPlugins\user')
            sys.path_importer_cache.clear()
            if 'tqcenter' in sys.modules:
                del sys.modules['tqcenter']
            
            if os.path.exists(tdx_api_path):
                sys.path.insert(0, tdx_api_path)
            
            from tqcenter import tq, tqconst
            self.tq = tq
            self.tqconst = tqconst
            
            # 初始化并获取账户句柄
            tdx_plugin_path = os.path.join(tdx_api_path, 'tqcenter.py')
            tq.initialize(tdx_plugin_path)
            self.tdx_account_id = tq.stock_account()
            
            if self.tdx_account_id and self.tdx_account_id > 0:
                self.tdx_available = True
                logger.info(f"{'='*50}\n[TradeManager] 已挂载【通达信】交易通道 (账户句柄: {self.tdx_account_id})\n{'='*50}")
            else:
                logger.warning("[TradeManager] 通达信账户句柄获取失败")
                
        except ImportError as e:
            logger.warning(f"[TradeManager] 未检测到新版通达信环境(tqcenter): {e}")
        except Exception as e:
            logger.warning(f"[TradeManager] 通达信模块跳过加载: {e}")

    def _init_guojin_qmt(self):
        try:
            # ====================== 国金 QMT 路径与环境配置 ======================
            QMT_INSTALL_PATH = r"D:\GJQMT"
            if os.path.exists(QMT_INSTALL_PATH):
                if QMT_INSTALL_PATH not in sys.path:
                    sys.path.append(QMT_INSTALL_PATH)
                    sys.path.append(os.path.join(QMT_INSTALL_PATH, "lib"))
                    sys.path.append(os.path.join(QMT_INSTALL_PATH, "bin.x64"))
                    sys.path.append(os.path.join(QMT_INSTALL_PATH, "bin.x64", "Lib", "site-packages"))
                
                from xtquant import xttrader, xtconstant
                from xtquant.xttype import StockAccount
                
                qmt_path = os.path.join(QMT_INSTALL_PATH, 'userdata_mini')
                session_id = int(time.time())
                self.xt_trader = xttrader.XtQuantTrader(qmt_path, session_id)
                self.xt_account = StockAccount(GJS_ACCOUNT)
                self.xtconstant = xtconstant
                
                self.xt_trader.start()
                connect_result = self.xt_trader.connect()
                if connect_result == 0:
                    self.xt_trader.subscribe(self.xt_account)
                    self.xtquant_available = True
                    logger.info(f"[TradeManager] 已挂载【国金MiniQMT】原生直连通道 (账号:{self.xt_account.account_id})")
                else:
                    logger.warning(f"[TradeManager] 国金QMT客户端连接失败 (错误码: {connect_result})")
        except Exception as e:
            logger.info(f"[TradeManager] 国金QMT模块跳过加载: {e}")

    def send_order(self, broker, action, symbol, volume, price, account_id=None):
        """暴露给外部的统一路由函数"""
        if broker == 'yinhe_qmt':
            # Try-read-OK 模式（v2 - 2026-06-15）
            # 连接 Test_Yinhe_qmt_ServerV5.py (8888)，主线程队列架构。
            # 服务端秒回 OK（入队后立即返回），所以发送后尝试读取回执。
            # 超时或失败时降级为 fire-and-forget（前端不卡死），兼顾可靠性与健壮性。
            try:
                if account_id:
                    cmd_str = f"{action},{symbol},{volume},{price},{account_id}\n"
                else:
                    cmd_str = f"{action},{symbol},{volume},{price}\n"
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client.settimeout(3.0)  # 3 秒内连不上就放弃
                client.connect(('127.0.0.1', 8888))
                client.sendall(cmd_str.encode('utf-8'))

                # 尝试读回执（1.5s 超时），读到 OK 可确认识别已送达引擎
                try:
                    client.settimeout(1.5)
                    resp = client.recv(1024).decode('utf-8').strip()
                    if resp == 'OK':
                        client.close()
                        return True, "银河QMT下单成功 (回执确认)"
                    client.close()
                    return True, f"银河QMT下单指令已发送 (回执: {resp})"
                except socket.timeout:
                    client.close()
                    return True, "银河QMT下单指令已发送 (fire-and-forget)"
                except Exception:
                    client.close()
                    return True, "银河QMT下单指令已发送"

            except ConnectionRefusedError:
                # 端口被占但连接被拒 → 可能全是僵尸线程，建议重启 QMT
                return False, "银河QMT未开启或 8888 桥接策略未运行（如多次重载策略后出现此错误，请重启QMT）"
            except Exception as e:
                return False, f"银河QMT下单异常: {str(e)}"
                
        elif broker == 'guojin_qmt':
            if not self.xtquant_available: return False, "国金QMT接口未就绪"
            try:
                # 转换买卖方向
                order_type = self.xtconstant.STOCK_BUY if action == 'BUY' else self.xtconstant.STOCK_SELL
                
                # 调用国金下单接口
                order_id = self.xt_trader.order_stock(
                    self.xt_account, 
                    symbol, 
                    order_type, 
                    int(volume), 
                    self.xtconstant.FIX_PRICE, 
                    float(price), 
                    "LOF_Arb", 
                    "API下单"
                )
                if order_id != -1:
                    return True, f"国金QMT下单成功，委托编号: {order_id}"
                else:
                    return False, "国金QMT下单失败（返回编号 -1）"
            except Exception as e:
                return False, f"国金QMT下单异常: {e}"
                
        elif broker == 'tdx':
            if not self.tdx_available: return False, "通达信接口未就绪"
            try:
                # 转换买卖方向: BUY=0(买入), SELL=1(卖出)
                order_type = self.tqconst.STOCK_BUY if action == 'BUY' else self.tqconst.STOCK_SELL
                
                # 调用通达信下单接口
                result = self.tq.order_stock(
                    account_id=self.tdx_account_id,
                    stock_code=symbol,        # 动态基金代码，如 "162411.SZ"
                    order_type=order_type,
                    order_volume=int(volume),
                    price_type=self.tqconst.PRICE_MY,  # 限价单
                    price=float(price)
                )
                
                # 解析返回结果
                error_id = result.get('ErrorId', -1)
                msg = result.get('Msg', '未知')
                
                if result.get('Value') in [1, 2] or error_id == 0:
                    wtbh = result.get('Wtbh', '')
                    return True, f"通达信下单成功，委托编号: {wtbh}"
                else:
                    return False, f"通达信下单失败: {msg}"
                    
            except Exception as e:
                return False, f"通达信下单异常: {str(e)}"
                
        return False, f"未知的通道标识: {broker}"