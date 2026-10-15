# -*- coding: utf-8 -*-
"""
[AI-2026-10-09] ETFarb → LOFarb 估值引用层（商品期货 F 类专属）。

背景（东哥 10-09 拍板）
----------------------
白银 `161226` 的估值（AG0 沪银期货推流）在 **LOFarb** 里已做得完善：
  · `SSEFuturesReader`（东财 SSE 推流，**强制 IPv4**）+ 新浪 `nf_AG0` 双源
  · 三种估值：`rt_val`（AG0实时/昨结算）、`static_val`（结算价 D/D-1）、`si_val`（COMEX 交叉验证）
  · 详见 `LOFarb/docs/010_白银期货功能说明.md`
⇒ **不复制、不重写**（移仓 = 重写 SSE 线程 + 三估值 + 比价面板，收益 0 风险极高）。
   ETFarb 侧只做**引用**：拿 LOFarb 已算好的 `rt_val` / `static_val` 展示。

豆粕 `159985` 与白银**同口径**（期货价 ÷ 昨结算），也放 LOFarb，一并引用。
能源化工 `159981` 走 **PCF 篮子口径**（需 PCF 指定合约月份 + 手数 + 乘数），
  由 **ETFarb 自己算**；本机新浪只给主连、取不到单合约 ⇒ **暂时算不出来，显「—」（宁缺勿假）**。

接口契约（实测 2026-10-09 11:2x，本机 LOFarb `http://127.0.0.1:8101/api/dashboard`）
------------------------------------------------------------------------
返回 `{"status":"ok","data":[{fund_code, fund_name, category, price, nav, nav_date,
  rt_val, rt_premium, static_val, static_premium, si_val, si_premium,
  ag0_price, ag0_settlement, realtime_price, prev_close, pos_ratio, ...}, ...]}`

口径红线
--------
  * **只引用，不重算**：本模块不做任何估值计算，只搬运 LOFarb 的结果。
  * **宁缺勿假**：LOFarb 不可达 / 行不存在 / 字段为空 ⇒ 一律返回 `None`，
    前端显「—」。**绝不**用现价/净值/旧值顶替（那会把「缺数据」伪装成「没动」）。
  * **失败不刷屏**：LOFarb 挂掉是常态事件（它可能没起），故按 code 去重日志。
  * **短 TTL 缓存**：LOFarb 自带 30s 口径，ETFarb 侧再缓存 30s 即可对齐，不重复压它。
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

# ---- LOFarb 地址（可环境变量覆盖）----
#   ⚠️ 端口实测（10-09）：本机 LOFarb/ArbDashboard 后端 = **8000**
#      （`main.py` 末尾 `uvicorn.run(..., port=8000)`；ARM 上也是 8000，由 nginx 代理到 /jsl/）。
#      `8101` 是 QMT/ETF_arb_Yinhe_QMT.py 用的另一个后端，**不是** LOFarb。
#   ⇒ 做成「候选列表 + 首次探测成功即记住」，避免写死端口导致换部署就断。
LOFARB_CANDIDATES = []
_env_base = os.environ.get("LOFARB_BASE_URL", "").strip()
if _env_base:
    LOFARB_CANDIDATES.append(_env_base.rstrip("/"))
LOFARB_CANDIDATES += ["http://127.0.0.1:8000", "http://127.0.0.1:8101"]

_base = {"url": LOFARB_CANDIDATES[0] if LOFARB_CANDIDATES else "http://127.0.0.1:8000"}

# ---- 本模块负责引用的标的（商品期货 F 类）----
#   白银 161226：估值在 LOFarb（SSE 沪银 AG0 推流 + 新浪 nf_AG0 双源，东哥 10-09 确认已完善），本模块只引用。
#   豆粕 159985：【东哥 10-09 拍板路 B 暂缓】——估值需「大商所豆粕期货 M」驱动，但 LOFarb 的
#     fund_service.py 只给 161226 写了 AG0 分支（if code == '161226'），159985 在 LOFarb 无
#     unified_fund_list 行、无 M 行情源、无估值分支 ⇒ 引用必拿空。暂不进 F TAB（fund_profile
#     jsl_market 已置 NULL），待 M 期货源（新浪 nf_IM/大商所主连实测存在）落实、LOFarb 补
#     159985 的 M 驱动分支后，再把 159985 加回 REFERENCED_CODES 并归 F。
#   能源化工 159981：【东哥 10-09 拍板删除】PCF 单合约源缺失（本机新浪只给主连），已删。
#
# 现状（2026-10-09 路 B 全部落地）：
#   豆粕 159985：LOFarb 已落实「大商所豆粕期货 M」驱动（新浪 nf_M0 豆粕连续，
#     字段位与 AG0 对称：最新价[8]/VWAP[9]/昨结算[10]），fund_service.py 已加
#     `if code == '159985'` M 驱动估值分支，unified_fund_list category='商品' 已插行，
#     fund_profile jsl_market='F' 已恢复。⇒ 加回引用并归 F。
REFERENCED_CODES = ("161226", "159985")

# 缓存与超时
_TTL = 30.0                 # 秒；对齐 LOFarb 自身 30s 口径
_TIMEOUT = 20               # 单次请求超时（LOFarb 的 build 可能较慢）
_lock = threading.Lock()
_cache = {"at": 0.0, "rows": None}
_logged = set()             # 失败去重：同一 code 只打一次，避免每轮询刷屏


def _log_once(key, level, msg, *args):
    if key in _logged:
        return
    if len(_logged) >= 256:
        _logged.clear()
    _logged.add(key)
    level(msg, *args)


def _fetch_rows(force=False):
    """取 LOFarb dashboard 行列表（带 30s 缓存）。失败返回 None。

    🔴 2026-10-09 改走 stdlib urllib.request（本机 Python 3.11 未装 requests，
       原 requests.Session 路径直接 _log_once('no_requests') 返回 None，F TAB 估值恒空）。
       urllib 显式 `proxies={}` 忽略 HTTP_PROXY（本机常有死代理，见 ETFarb 记忆）；
       跳过 SSL 校验（与 etf_iopv_quote 的 _CTX 同口径，本机自签/内网证书）。"""
    now = time.time()
    with _lock:
        if not force and _cache["rows"] is not None and now - _cache["at"] < _TTL:
            return _cache["rows"]
    # 先试上次成功的地址，再按候选列表轮换（探测成功后记住）
    order = [_base["url"]] + [u for u in LOFARB_CANDIDATES if u != _base["url"]]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理
    idx = None
    errs = []
    for base in order:
        if not base:
            continue
        try:
            req = opener.open(base + "/api/dashboard", timeout=_TIMEOUT)
            with req as r:
                if r.status != 200:
                    raise RuntimeError("HTTP %s" % r.status)
                body = r.read()
            rows = (json.loads(body.decode("utf-8")) or {}).get("data") or []
            idx = {}
            for row in rows:
                code = str(row.get("fund_code") or "").strip()
                if code:
                    idx[code] = row
            _base["url"] = base
            break
        except Exception as e:
            errs.append("%s:%s" % (base, repr(e)[:60]))
            continue
    if idx is None:
        _log_once("fetch", logger.warning,
                  "lofarb_bridge: LOFarb dashboard 不可达（%s），商品期货估值显「—」（不兜底）",
                  "; ".join(errs)[:200])
        return None
    with _lock:
        _cache["at"] = time.time()
        _cache["rows"] = idx
    return idx


def get_reference(code, force=False):
    """
    取 LOFarb 已算好的估值（**只引用，不重算**）。

    返回 dict（缺字段为 None）或 None（LOFarb 不可达 / 无该行）：
      {'rt_val','rt_premium','static_val','static_premium','si_val','si_premium',
       'ag0_price','ag0_settlement','nav','nav_date','price','realtime_price',
       'source':'LOFarb','category'}
    """
    code = str(code or "").strip()
    if code not in REFERENCED_CODES:
        return None
    idx = _fetch_rows(force=force)
    if not idx:
        return None
    row = idx.get(code)
    if not row:
        _log_once("norow:" + code, logger.info,
                  "lofarb_bridge: LOFarb dashboard 无 %s 的行（估值显「—」）", code)
        return None
    return {
        "rt_val": _num(row.get("rt_val")),
        "rt_premium": _num(row.get("rt_premium")),
        "static_val": _num(row.get("static_val")),
        "static_premium": _num(row.get("static_premium")),
        "si_val": _num(row.get("si_val")),
        "si_premium": _num(row.get("si_premium")),
        "ag0_price": _num(row.get("ag0_price")),
        "ag0_settlement": _num(row.get("ag0_settlement")),
        "m0_price": _num(row.get("m0_price")),
        "m0_settlement": _num(row.get("m0_settlement")),
        "nav": _num(row.get("nav")),
        "nav_date": row.get("nav_date"),
        "price": _num(row.get("price")),
        "realtime_price": _num(row.get("realtime_price")),
        "category": row.get("category"),
        "source": "LOFarb",
    }


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def diagnose():
    """体检：打印 LOFarb 连通性与各引用标的取值。"""
    idx = _fetch_rows(force=True)
    print("LOFarb 候选地址:", ", ".join(LOFARB_CANDIDATES))
    print("dashboard     :", ("✅ 通（%d 行）" % len(idx)) if idx else "❌ 不可达")
    print("实际命中       :", _base["url"])
    if not idx:
        return
    for code in REFERENCED_CODES:
        r = get_reference(code, force=True)
        print("  %-8s -> %s" % (code, r if r else "None（显「—」）"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    diagnose()
