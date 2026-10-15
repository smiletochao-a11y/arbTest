# -*- coding: utf-8 -*-
"""跨境 ETF 看板 —— 行情编排与行组装（P1，2026-09-17 立项，docs/40）。

🔴 **这不是第二个估值引擎。** 所有 IOPV 数字都出自 `core/iopv_calc.IOPVCalculator`；
本模块只做三件事：

  1. **集中取价** —— ETF 行情 / 港股行情 / 汇率 / 指数，各**一批一次**，
     避免 N 只标的各自请求（N=246 时逐只发请求 ≈ 500 个/轮，30 秒刷新扛不住）；
  2. **注入引擎** —— `calc_realtime(marks=…, fx_info=…, market_price=…)`，
     公式仍留在引擎里，这里只外包「取价」；
  3. **组装看板行** —— 集思录同名列 + 内部质量指标（`quote_path` 等**前端不渲染**）。

行情源（全部免费、无鉴权，本轮实测）
------------------------------------
| 用途 | 源 | 字段 |
|---|---|---|
| ETF 行情 | 腾讯 `qt.gtimg.cn/q=sh513090,sz159366,…`（**多码一次**） | `[3]`市价 `[32]`涨跌幅% `[37]`成交额(万元) `[77]`官方IOPV溢价率% `[78]`官方IOPV `[81]`NAV(D) |
| **净值/净值日** | **腾讯 `qt.gtimg.cn/q=jj513090,jj159366,…`（多码一次）**，见 `core/nav_quote` | `[5]`单位净值 `[6]`累计 `[8]`**净值日期**（= 集思录「最新已公布净值」口径，docs/40 §七·17） |
| 港股行情 | **新浪 `hq.sinajs.cn/list=rt_hk01093`（实时）**，见 `core/hk_quote` | `[3]`现价 `[19]`状态（`'20'`=停牌） |
| 汇率 | 新浪 `fx_shkdcny` 等（逐券共用一次） | `[1]`现价 |
| 指数 | `core/index_proxy`（腾讯美股/港股 + 东财国际） | 见该模块 |

🔴 **港股行情必须走实时的 `rt_hk`，不能用腾讯 `hk`**（2026-09-17 实测）：
后者是**15 分钟延迟行情**（时间戳恒定滞后 15 分 01 秒），拿它当实时价算逐券 IOPV
会把「15 分钟前的篮子」和「实时的 ETF 市价」相减，折溢价被污染 ——
实测绝对值中位误差 **5.4bp ⇒ 2.5bp**。判据与停牌口径见 `core/hk_quote.py` 文件头。

关于腾讯官方 IOPV（`[78]`/`[77]`）
---------------------------------
这是**免费的真值源**，本轮用它来量化自算精度。它在看板里**只作内部质量指标**
（`iopv_err_bps`），不参与展示，也不覆盖自算值 —— 自算是历史回补与篮子明细的唯一通道，
两条口径必须能互相印证，而不是互相遮蔽。

⚠️ **官方 IOPV 内部并存两套汇率口径，`iopv_err_bps` 天然是双峰，别去「修」**（2026-09-17 查清，
docs/40 §七·13）：池内 31 只的隐含汇率 = **实时市场汇率**（0.8548~0.8550 ≈ 新浪 `fx_shkdcny`
0.855044），另 4 只（`520920`/`159028`/`159120`/`159131`）的隐含汇率 = **chinamoney 中间价**
（0.8614~0.8636 ≈ 今日中间价 0.86154）⇒ 这 4 只与自算**结构性差 ~+76bp**，与取值正确性无关。
本模块**统一用实时市场汇率**（套利真实成交汇率），不逐只猜官方用了哪套。
"""
import datetime
import json
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PROJ = os.path.dirname(_HERE)
DATA_DIR = os.path.join(PROJ, "data")
DB_PATH = os.path.join(DATA_DIR, "etfarb.db")  # 占位，下方 import db 后重赋为 arb_master.db

# ---- 轻量文件日志（2026-10-08） ----
#   与 backend/app.py 的 `_logf` 同款、同文件（log/server.log），core 侧取数失败不再静默。
#   🔴 本模块以前所有网络失败都是 `except: pass`，事后**完全无法追溯**（亚洲 TAB 整列空查不到根因）。
LOG_DIR = os.path.join(PROJ, "log")
SERVER_LOG = os.path.join(LOG_DIR, "server.log")


def _logf(msg):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass

# ============ 盘后冻结快照（#19：完全仿照集思录，盘后不再跟美股走） ============
# 冻结窗口：交易日 16:20 起，到次日 09:30 开盘前。区间内 `idx_chg`/`iopv`/`iopv_prem`
# 三列改用「当日盘中最后一轮快照」，不再跟美股实时变动（与集思录盘后同口径）。
# 快照落盘（JSON 文件），使进程重启、机器不常开机也能读到 —— 非冻结时段每轮覆盖写入。
_FREEZE_SNAP_PATH = os.path.join(DATA_DIR, "etf_freeze_snapshot.json")
_FREEZE_LOCK = threading.Lock()
_FROZEN_FROM = (16, 20)          # (h, m) 起冻结
_LIVE_FROM = (9, 30)             # (h, m) 开盘，此前也冻结
_FREEZE_MAX_AGE_H = 24           # 快照超过 24h 视为过期 ⇒ 留空显「—」

import db as _db                       # noqa: E402
DB_PATH = _db.DB_PATH   # 单库方案：统一落 arb_master.db（与 arbTest 同文件）
from iopv_calc import IOPVCalculator, IOPVDataMissing   # noqa: E402
from index_proxy import IndexProxyQuote, IndexProxyMissing   # noqa: E402
from fx_provider import FXProvider, FXMissingError      # noqa: E402
from hk_quote import HKQuoteFetcher                     # noqa: E402
from nav_quote import NavQuoteFetcher                    # noqa: E402
from index_quote import IndexQuoteProvider                # noqa: E402
import static_val as _sv                                  # noqa: E402  静态估值引擎（09-18）
try:
    import saudi_bridge as _saudi          # 沙特双雄桥接（02830 × HKD/CNY）
except ImportError:
    from core import saudi_bridge as _saudi
from bond_etf_valuation import (estimate_bond_nav, get_treasury_futures_quote,  # noqa: E402
                                latest_static_bond)  # 现金管理 3 只债券估值（09-18/09-19）
try:                                                      # noqa: E402
    import iopv_pcf as _pcfurl          # 交易所官方 PCF 直链（09-28 东哥：主看板新增「PCF」列）
except Exception:                       # 独立运行/路径未配好时不致命
    _pcfurl = None

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

QT_URL = "https://qt.gtimg.cn/q="
_CHUNK = 60          # 单次请求的代码数（URL 长度安全上限）

VIRTUAL_CASH_CODE = "159900"


def _qt_prefix(code):
    """A 股 ETF 的腾讯前缀：沪市 sh / 深市 sz。"""
    return ("sh" if str(code)[:1] in "56" else "sz") + str(code)


def _num(x, default=None):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def _iso(d):
    """'20260916' / '2026-09-16' → '2026-09-16'。"""
    s = str(d or "").strip().replace("-", "")
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 and s.isdigit() else (str(d) if d else "")


def _conn():
    return sqlite3.connect(DB_PATH)


class ETFMarketBoard:
    """跨境 ETF 看板数据装配。一次 `build()` = 一轮完整快照（30 秒级刷新安全）。"""

    def __init__(self, db=DB_PATH):
        self.db = db
        self.proxy = IndexProxyQuote()
        self.hk = HKQuoteFetcher()          # 🔴 默认走新浪实时口（见文件头）
        self.nav_quote = NavQuoteFetcher()  # 最新已公布净值（腾讯 jj，与集思录同口径）
        self.idx_provider = IndexQuoteProvider()  # LOF 估值用的指数涨跌幅通道
        self._scaler_cache = {}       # (index_key, fx_pair) -> scaler dict
        self._fx_cache = {}           # pair -> 市场汇率 dict
        self._comp_cache = {}         # fund_code -> 最新公告成分码
        self._subst_cache = {}        # fund_code -> (必须现金替代成分码, 篮子基准日 D)

    # ------------------------------------------------------------------
    # 低层取数
    # ------------------------------------------------------------------
    # ---- 腾讯行情批取：三级加固（2026-10-08，东哥「亚洲 TAB 盘后整列是 —」） ----
    #   🔴 事故回放：`build()` 用 `qt.gtimg.cn?q=…` 多码批量取全部 ETF 行情（`_CHUNK=60`，
    #      亚洲 219 只 = 4 批）。原来**任一批失败就 `continue`** ⇒ 该批 60 只同时 `price=None`
    #      ⇒ `iopv_prem` 整列全空 ⇒ **该 TAB 那一列全是「—」**（单点故障被放大成一列）。
    #      21:5x 实测即如此（219/219 price 为空、官方 IOPV 也空），而腾讯接口随后完全正常
    #      （3 轮 × 4 批全 219/219、1~3 秒）⇒ 偶发抖动，不是源坏了。
    #   放大器：A 一次 build 要 130~200s（docs/40 §四），而后台预热线程 `sleep(20)`
    #      **不计入装配耗时** ⇒ 几乎从不喘息；叠加前台 SWR 重建 ⇒ 多线程同时打同一批数据源。
    #   现在三级：① 退避重试 ② 仍失败则**二分降级**（故障面 60 → ≤30 → ≤15）
    #      ③ 全程写日志（批大小/异常/丢失码），不再静默。
    _QT_RETRY = 2                    # 首次失败后的重试次数（合计最多 3 次请求）
    _QT_BACKOFF = (0.6, 1.5)         # 退避秒数
    _QT_SPLIT_DEPTH = 2              # 二分降级最大层数（60 → 30 → 15 只/批）

    def _qt_fetch(self, batch):
        """取一批腾讯行情。返回 `(dict, None)` 成功 / `(None, 异常)` 失败。"""
        url = QT_URL + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://finance.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=20, context=_CTX).read().decode(
                "gbk", "ignore")
        except Exception as e:
            return None, e
        out = {}
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r'v_([A-Za-z0-9_]+)="(.*)"$', line)
            if m:
                out[m.group(1)] = m.group(2).split("~")
        return out, None

    def _qt_batch(self, qcodes, _depth=0):
        """腾讯行情批量取：一次请求最多 `_CHUNK` 个码。

        返回 `{qcode: [字段列表]}`。取不到的码**不进结果**（调用方按缺失处理，不兜底）。
        🔴 单批失败**不再整批丢弃**（那会把 60 只一起打成「整列 —」）：退避重试 → 二分降级 → 才丢。"""
        out = {}
        qcodes = [c for c in qcodes if c]
        for i in range(0, len(qcodes), _CHUNK):
            batch = qcodes[i:i + _CHUNK]
            got, err = None, None
            for attempt in range(self._QT_RETRY + 1):
                if attempt:
                    time.sleep(self._QT_BACKOFF[min(attempt - 1, len(self._QT_BACKOFF) - 1)])
                got, err = self._qt_fetch(batch)
                if got is not None:
                    break
            if got is not None:
                out.update(got)
                continue
            msg = "腾讯行情批失败 size=%d depth=%d err=%s" % (len(batch), _depth, repr(err)[:120])
            if _depth < self._QT_SPLIT_DEPTH and len(batch) > 1:
                _logf(msg + " → 二分降级重试")
                half = len(batch) // 2
                for part in (batch[:half], batch[half:]):
                    sub = self._qt_batch(part, _depth + 1)
                    if sub:
                        out.update(sub)
            else:
                _logf(msg + " → 该批 %d 只将无价（宁缺勿假，不跨源兜底）" % len(batch))
        return out

    def _comp_codes(self, fund_code):
        """该基金**最新公告**的成分码（排除深市虚拟行 159900）。"""
        c = _conn()
        td = c.execute("SELECT MAX(trade_date) FROM pcf_component WHERE fund_code=?",
                       (fund_code,)).fetchone()
        if not td or not td[0]:
            c.close()
            self._comp_cache[fund_code] = []
            return []
        rows = c.execute(
            "SELECT DISTINCT stock_code FROM pcf_component WHERE fund_code=? AND trade_date=? "
            "AND stock_code<>?", (fund_code, td[0], VIRTUAL_CASH_CODE)).fetchall()
        c.close()
        out = [r[0] for r in rows if r[0]]
        self._comp_cache[fund_code] = out
        return out

    def _subst_codes(self, fund_code):
        """该基金最新公告里「**必须现金替代**」成分的代码 + 其篮子基准日 D。

        🔴 这类成分 `shares`=0、货值落在 `cash_value`（深市 PCF 的『申购替代金额』列），
          引擎要把它们按「D 货值 ÷ (D 日未调整收盘 × 汇率)」折成等效股数并入篮子，
          否则 IOPV 整块偏低（docs/40 §七·15）。看板**批量预热** D 日收盘价用这个清单，
          `IOPVCalculator.calc_realtime` 之后就能命中 `hk_quote` 的内存日K缓存。

        返回 (codes, basket_td)；无此类成分时 ([], None)（= 绝大多数标的，上海 XML 恒为 None）。"""
        if fund_code in self._subst_cache:
            return self._subst_cache[fund_code]
        res = ([], None)
        try:
            c = _conn()
            td = c.execute("SELECT MAX(trade_date) FROM pcf_component WHERE fund_code=?",
                           (fund_code,)).fetchone()
            if td and td[0]:
                codes = [r[0] for r in c.execute(
                    "SELECT stock_code FROM pcf_component WHERE fund_code=? AND trade_date=? "
                    "AND cash_value IS NOT NULL AND cash_value>0", (fund_code, td[0])).fetchall()]
                bd = c.execute("SELECT pre_trade_date FROM pcf_snapshot "
                               "WHERE fund_code=? AND trade_date=?", (fund_code, td[0])).fetchone()
                if codes:
                    res = (codes, (bd[0] if bd else None))
            c.close()
        except Exception:
            pass
        self._subst_cache[fund_code] = res
        return res

    def _nav_map(self, codes):
        """批量取「**最新已公布净值** + 其净值日期」，`{code: (nav, nav_date, src)}`。

        🔴 为什么单独批取（2026-09-17 修 #16b）：净值列（`nav`/`nav_date`/`nav_prem`）是
          **基金自身的属性**，**与走哪条 IOPV 通道无关**。原先它只在算完 IOPV 之后赋值，
          于是 `no_source`（无免费指数源）与 `frozen`（成分零成交）两个分支 `continue`
          一走，「净值溢价率」就整列空掉 —— 集思录对这些全都有值。
          一次批取，路径解耦，任何分支都拿得到。

        🔴 口径改为「最新已公布」（2026-09-17 修 #17，东哥拍板）：
          主源 = 腾讯 `jj`（`core/nav_quote`，5 请求 / 1.1s 全池），与**集思录同口径**
          （集思录 `fund_nav` 实测 ≡ `jj[5]`，日期 ≡ `jj[8]`）。
          `src='jj'`。取不到的码**回退** PCF 头部 `<NAV>` + `pre_trade_date`（= 基准日 D，
          `src='pcf'`）——回退只保证「有值」，语义是 D 日，故 `nav_src` 一并暴露给上层。

        ⚠️ **时点相关**：盘中 `jj[8]` 恰好 = D（今日净值尚未公布），与旧口径一致；
           盘后公布后自动前进一步 ⇒ **不需要任何时点判断**。
        ⚠️ 池内 246 只 PCF 全有 nav（09-17 实测），故回退路径恒可用。"""
        if not codes:
            return {}
        codes = list(codes)

        # ---- 1) 回退基线：最新一份 PCF 的 <NAV> + 基准日 D ----
        out = {}
        c = _conn()
        try:
            ph = ",".join("?" * len(codes))
            rows = c.execute(
                f"SELECT s.fund_code, s.nav, s.pre_trade_date FROM pcf_snapshot s "
                f"JOIN (SELECT fund_code, MAX(trade_date) AS td FROM pcf_snapshot "
                f"      WHERE fund_code IN ({ph}) GROUP BY fund_code) m "
                f"  ON m.fund_code = s.fund_code AND m.td = s.trade_date",
                codes).fetchall()
            for fc, nav, bd in rows:
                out[fc] = (nav, _iso(bd) if bd else None, "pcf")
        finally:
            c.close()

        # ---- 2) 主源：腾讯 jj 覆盖（取不到的码保留回退值）----
        try:
            jj = self.nav_quote.batch_latest(codes)
        except Exception:
            jj = {}
        for fc, info in jj.items():
            out[fc] = (info["nav"], info["nav_date"], "jj")
        return out

    def _fx_spot(self, pair):
        """实时市场汇率（逐券共用；按币种缓存一轮）。"""
        if pair not in self._fx_cache:
            self._fx_cache[pair] = FXProvider("", self.db).get_spot_rate_info(pair)
        return self._fx_cache[pair]

    def _cfg_map(self, codes):
        """fund_iopv_config 的 `fx_currency` / `pcf_source` 批量映射 `{code: (fx, src)}`。"""
        out = {}
        if not codes:
            return out
        c = _conn()
        try:
            ph = ",".join("?" * len(codes))
            for fc, fx, src in c.execute(
                    f"SELECT fund_code, fx_currency, pcf_source FROM fund_iopv_config "
                    f"WHERE fund_code IN ({ph})", codes):
                out[fc] = (fx, src)
        finally:
            c.close()
        return out

    def _settle_map(self, keys):
        """批量取「买入结算汇兑比率」最新一条（`rate_date` < 今日，即 T-1 可得值）。

        🔴 取日口径与 `backend.app._get_settlement_fx` 一致：`<` 不带当日——
        适用日 D 的结算汇率 D 当天盘后才公布，盘中只能拿 T-1（docs/21 §settle）。
        返回 `{ccy_pair: (rate, rate_date)}`；没数据的键不进结果（绝不兜底）。"""
        out = {}
        keys = [k for k in keys if k]
        if not keys:
            return out
        today = datetime.date.today().isoformat()
        c = _conn()
        try:
            ph = ",".join("?" * len(keys))
            for k, rate, rd in c.execute(
                    f"SELECT ccy_pair, rate, rate_date FROM fx_rate "
                    f"WHERE ccy_pair IN ({ph}) AND rate_date<? ORDER BY rate_date",
                    keys + [today]):
                out[k] = (float(rate), rd)          # ORDER BY 后写覆盖 ⇒ 每键留最新
        finally:
            c.close()
        return out

    def _scaler(self, index_key, fx_pair):
        """指数比 × 汇率比（按 (index_key, fx_pair) 缓存一轮）。"""
        k = (index_key, fx_pair)
        if k not in self._scaler_cache:
            self._scaler_cache[k] = self.proxy.get_basket_scaler(index_key, fx_pair)
        return self._scaler_cache[k]

    # ------------------------------------------------------------------
    # 池
    # ------------------------------------------------------------------
    def pool(self, market=None, fund_type=None, limit=None, min_share=None, codes=None):
        """看板标的：`fund_profile` + 最新份额。按份额降序（规模优先）。

        `market`：'A'=亚洲(ETF+QDII亚洲混合) / 'E'=欧美 / 'L'=国内LOF / 'C'=现金管理
                  / 'G'=黄金原油(商品LOF，arbTest woody 覆盖) / 'WATCH'=关注池（跨市场，按 watch_pool 表过滤，测试看板用）。
        `fund_type`：'ETF' / 'LOF'（前端「显示ETF/显示LOF」复选框用）。
        `min_share`（万份）用于「先做达标的子集」—— 不传即全池。"""
        c = _conn()
        sql = ("SELECT fund_code, jsl_market, fund_name, company, jsl_index_nm, "
               "apply_fee, apply_status, redeem_fee, redeem_status, mgmt_fee, "
               "quote_path, ncomp, low_cov, fund_type, category, idx_code, idx_name, "
               "val_method, in_arbtest "
               "FROM fund_profile")
        cond, args = [], []
        if market == "WATCH":
            # 🔴 关注池（2026-09-26 东哥需求）：测试看板只显示「喂过数据/分析过、有可能套利」
            #    的精选标的，而非全市场。来源 = watch_pool 配置表（跨市场，故不按 jsl_market 过滤）。
            #    排序标签由 watch_pool.sort 决定（前端仍可再点列头排序）。
            cond.append("fund_code IN (SELECT code FROM watch_pool)")
        elif market:
            cond.append("jsl_market=?")
            args.append(market)
        if fund_type:
            cond.append("fund_type=?")
            args.append(fund_type)
        if codes:
            # 🔴 自选合并：market=WATCH 时返回「关注池 ∪ 前端自选(localStorage)」，
            #    自选基金的完整行（IOPV/溢价/份额）由服务端补齐。
            #    WATCH 与 codes 必须 OR 并集（否则 AND 变交集，自选基金被滤掉）；
            #    非 WATCH + codes 则为 AND 交集（按需，一般不用）。
            ph = ",".join("?" * len(codes))
            codes_cond = "fund_code IN (" + ph + ")"
            if market == "WATCH" and cond:
                cond[0] = "(" + cond[0] + " OR " + codes_cond + ")"
            else:
                cond.append(codes_cond)
            args.extend([str(c).strip() for c in codes])
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        cols = ["fund_code", "jsl_market", "fund_name", "company", "jsl_index_nm",
                "apply_fee", "apply_status", "redeem_fee", "redeem_status", "mgmt_fee",
                "quote_path", "ncomp", "low_cov", "fund_type", "category",
                "idx_code", "idx_name", "val_method", "in_arbtest"]
        rows = [dict(zip(cols, r)) for r in c.execute(sql, args)]
        # 🔴 关注标记：watched = code 是否在 watch_pool（所有市场统一，供前端星标显示）
        watched_codes = {r[0] for r in c.execute("SELECT code FROM watch_pool")}
        # 关注池：带出标签 / 备注 / 排序（供前端渲染「标签」列）
        watch_meta = {}
        if market == "WATCH":
            for wc in c.execute("SELECT code, tag, sort, note FROM watch_pool"):
                watch_meta[wc[0]] = {"watch_tag": wc[1], "watch_sort": wc[2], "watch_note": wc[3]}
            for r in rows:
                r.update(watch_meta.get(r["fund_code"],
                                        {"watch_tag": None, "watch_sort": 999, "watch_note": None}))
        for r in rows:
            r["watched"] = r["fund_code"] in watched_codes
        # 份额（统一宽表 unified_fund_history：shares=pre_share 万份，shares_added=new_share）
        shares = {}
        for r in c.execute("SELECT fund_code, date, shares, shares_added FROM unified_fund_history "
                           "WHERE (fund_code, date) IN (SELECT fund_code, MAX(date) "
                           "FROM unified_fund_history GROUP BY fund_code)"):
            shares[r[0]] = {"share_date": r[1], "pre_share": r[2], "new_share": r[3]}
        c.close()
        for r in rows:
            r.update(shares.get(r["fund_code"], {"share_date": None, "pre_share": None,
                                                 "new_share": None}))
        if min_share is not None:
            rows = [r for r in rows if (r["pre_share"] or 0) >= min_share]
        if market == "WATCH":
            # 关注池默认按 watch_pool.sort 排序（重点在前），前端仍可点列头重排
            rows.sort(key=lambda r: (r.get("watch_sort") or 999))
        else:
            rows.sort(key=lambda r: -(r["pre_share"] or 0))
        return rows[:limit] if limit else rows

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    # ---------- 盘后冻结（#19） ----------
    @staticmethod
    def _is_frozen_mode(now=None):
        """交易日 16:20 起、到次日 09:30 前 ⇒ 冻结（与集思录盘后同口径）。"""
        now = now or datetime.datetime.now()
        h, m = now.hour, now.minute
        if (h, m) >= _FROZEN_FROM:          # 16:20 起
            return True
        if (h, m) < _LIVE_FROM:            # 次日 09:30 前
            return True
        return False

    @staticmethod
    def _snap_fresh(v, now):
        ts = (v or {}).get("ts")
        if not ts:
            return False
        try:
            t = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
        return (now - t).total_seconds() < _FREEZE_MAX_AGE_H * 3600

    @staticmethod
    def _load_freeze_snap():
        try:
            with open(_FREEZE_SNAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _save_freeze_snap(snap):
        try:
            os.makedirs(os.path.dirname(_FREEZE_SNAP_PATH), exist_ok=True)
            tmp = _FREEZE_SNAP_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, _FREEZE_SNAP_PATH)
        except Exception:
            pass

    def build(self, market=None, fund_type=None, limit=None, min_share=None,
              with_official=True, force_live=False, codes=None):
        """组装一轮看板快照。

        返回 `{'ts', 'market', 'rows', 'stats', 'errors'}`。
        每行**必带** `quote_path`（内部消费，前端不渲染）。

        `market`：'A'/'E'/'L'/'C'（见 `pool`）。
        `fund_type`：'ETF'/'LOF'（前端「显示ETF/显示LOF」复选框）。
        `force_live=True` 跳过盘后冻结（仅供开发自测 / 调试，生产调用不要传）。
        """
        profs = self.pool(market, fund_type, limit, min_share, codes=codes)
        if not profs:
            return {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "market": market, "rows": [], "stats": {}, "errors": []}
        codes = [p["fund_code"] for p in profs]

        # ---- 盘后冻结闸门（#19，2026-09-19 口径收敛） ----
        #   冻结时三列改用盘中快照；非冻结时正常算并把本轮结果写入快照（供盘后读取）。
        #   🔴 仅欧美(E)盘后冻结：其成分含美股(盘后至凌晨4点)/欧股(北京凌晨收)，
        #      盘后仍在波动 ⇒ 自算 IOPV 漂移为假溢价。国内LOF(L)/亚洲(A)成分均北京
        #      白天收盘(港股16:00/A股15:00/日经14:00)，盘后不动，自算溢价即真实收盘值，
        #      无需冻结；现金管理(C)本就走 bond 通道豁免。market=None(CLI 全市场调试)
        #      保守按冻结处理，避免欧美失保护。
        #   WATCH 关注池跨市场（含欧美成分），盘后同样需冻结保护 ⇒ 与 E 同待遇。
        frozen = (not force_live) and self._is_frozen_mode() \
            and (market is None or market in ("E", "WATCH"))
        snap_valid = {}
        if frozen:
            now0 = datetime.datetime.now()
            snap_valid = {c: v for c, v in self._load_freeze_snap().items()
                          if self._snap_fresh(v, now0)}

        # ---- 0) 最新已公布净值 + 净值日期：**与 IOPV 通道无关**，一次批取 ----
        #   放在分流之前算完 ⇒ no_source / frozen / err 任何分支都带着净值出门
        #   （2026-09-17 修：原先只在算完 IOPV 后赋值，两个 continue 分支把
        #    「净值溢价率」整列带空了，E 19 只、A 2 只 —— 见 `_nav_map`）
        nav_map = self._nav_map(codes)

        # ---- 0a-2) 官方 PCF 直链（2026-09-28 东哥：主看板新增「PCF」列，放官方IOPV 右边）----
        #   深市需「最近可得交易日」，全池**共用一次探测**（模块内 30min 缓存）；
        #   沪市无日期参数（永远最新）。无官方源的标的 pcf_url=None（前端显「—」）。
        #   ⚠️ 探测失败绝不阻断看板：异常吞掉，退回「按前缀给链接」。
        pcf_szse_date = None
        if _pcfurl and any(_pcfurl.is_szse_etf(c) for c in codes):
            try:
                pcf_szse_date = _pcfurl.latest_szse_pcf_date()
            except Exception:
                pcf_szse_date = None

        # ---- 0b) LOF 静态估值数据：过期即补（净值 year_all + 指数日K，12h TTL）----
        #   首轮抓取约 10s（41 净值 + ~35 指数K线），之后 12h 内零网络开销。
        lof_profs = [p for p in profs if (p.get("fund_type") or "ETF") == "LOF"
                     or (p.get("jsl_market") or "") == "C"]
        if lof_profs:
            try:
                _sv.ensure_nav_history([p["fund_code"] for p in lof_profs], self.db)
                _sv.ensure_index_history([p.get("idx_code") for p in lof_profs
                                          if p.get("idx_code")], self.db)
            except Exception:
                pass

        # ---- 1) ETF 行情：一批（或几批）全取 ----
        etf_q = {c: _qt_prefix(c) for c in codes}
        etf_raw = self._qt_batch(list(etf_q.values()))

        # ---- 2) 逐券类：成分港股行情去重后批量取（**实时口**） ----
        perstock = [p for p in profs if (p["quote_path"] or "") == "perstock"]
        comps_map, need_hk = {}, set()
        for p in perstock:
            cs = self._comp_codes(p["fund_code"])
            comps_map[p["fund_code"]] = cs
            need_hk.update(cs)
        # 🔴 走 hk_quote 的**实时**口（新浪 rt_hk）。腾讯口是 15 分钟延迟行情，
        #    用它会把「15 分钟前的篮子」与实时市价相减 ⇒ 折溢价被污染（见文件头）。
        #    `detail=True` 额外取回成交量/今开，供下面判「报价冻结」（单券 FoF 用）。
        marks_all = self.hk.get_realtime_marks(sorted(need_hk), detail=True) if need_hk else {}

        # ---- 2b) 「必须现金替代」成分的 D 日未调整收盘价：**去重后一次取完** ----
        #   引擎按「D 货值 ÷ (D 日未调整收盘 × 汇率)」把它们折成**等效股数**并入实时篮子
        #   （2026-09-17 修 docs/40 §七·15：漏了它们自算 IOPV 整块偏低，159960 曾 −767bp）。
        #   这里一次取完再按基金分发（`calc_realtime(d_close=…)`），避免每只基金各发一遍请求。
        per_fund_cv, cv_dates = {}, set()
        for p in perstock:
            cs, bd = self._subst_codes(p["fund_code"])
            if cs and bd:
                per_fund_cv[p["fund_code"]] = (cs, bd)
                cv_dates.add(bd)
        subst_d = {}
        for bd in cv_dates:
            iso = _iso(bd)
            need = sorted({c for cs, b in per_fund_cv.values() if b == bd for c in cs})
            try:
                rawd = self.hk.get_raw_closes(need, iso, iso)
            except Exception:
                continue        # 取不到就不注入 ⇒ 引擎自己再试，仍失败则抛错（绝不兜底）
            for fc, (cs, b) in per_fund_cv.items():
                if b == bd:
                    subst_d[fc] = {c: (rawd.get(c) or {}).get(iso) for c in cs}

        # ---- 3) 汇率：逐券类共用 HKD/CNY，一次 ----
        fx_hkd, fx_err = None, None
        if perstock:
            try:
                fx_hkd = self._fx_spot("HKD/CNY")
            except FXMissingError as e:
                fx_err = str(e)

        # ---- 4) 逐只算 IOPV + 组装 ----
        rows, errors = [], []
        stat = {"perstock": 0, "proxy": 0, "no_source": 0, "frozen": 0, "err": 0,
                "lof": 0, "official_cmp": 0, "err_bps": []}
        for p in profs:
            code = p["fund_code"]
            path = (p["quote_path"] or "")
            f = etf_raw.get(etf_q[code])
            row = {
                "code": code,
                "name": p["fund_name"],
                "market": p["jsl_market"],
                # 关注池标签（仅 market=WATCH 时由 pool() 注入；其它市场为 None，前端不渲染）
                "watch_tag": p.get("watch_tag"),
                "watch_note": p.get("watch_note"),
                "watch_sort": p.get("watch_sort"),
                # 🔴 关注标记：watched = code 是否在 watch_pool（pool() 已注入，build() 重组行时务必透传，
                #    否则「我的关注」页按 r.watched 过滤会全空；市场 TAB 也靠它亮星）。
                "watched": p.get("watched"),
                # 🔴 内部分流结果：**前端不渲染**，供后端/调试判断走的是哪条通道
                "quote_path": path or None,
                "fund_type": p.get("fund_type") or "ETF",
                "ncomp": p["ncomp"],
                "low_cov": p["low_cov"],
                # 集思录展示列（静态部分，来自 fund_profile）
                "index_nm": p["jsl_index_nm"],
                "sub_fee": p["apply_fee"], "sub_status": p["apply_status"],
                "red_fee": p["redeem_fee"], "red_status": p["redeem_status"],
                "mgmt_fee": p["mgmt_fee"], "company": p["company"],
                "pre_share": p["pre_share"], "pre_new": p["new_share"],
                "share_date": p["share_date"],
                # 行情（腾讯）
                "price": _num(f[3]) if f and len(f) > 3 else None,
                "chg_pct": _num(f[32]) if f and len(f) > 32 else None,
                "amount_wan": _num(f[37]) if f and len(f) > 37 else None,
                # 🔴 换手率改用成交量(手)口径（与集思录/LOF套利程序一致）；腾讯 f[6] 已是「手」，切勿再 ÷100
                "volume_lot": _num(f[6]) if f and len(f) > 6 else None,
                # 非展示列：自算 vs 官方（内部质量指标）
                "official_iopv": _num(f[78]) if (with_official and f and len(f) > 78) else None,
                "official_iopv_prem": _num(f[77]) if (with_official and f and len(f) > 77) else None,
                "official_nav_d": _num(f[81]) if (with_official and f and len(f) > 81) else None,
                # 交易所官方 PCF 直链（前端「PCF」列，2026-09-28）：
                #   深市 = CDN 宽表 txt（带最近交易日）／沪市 = 上交所下载口（无日期）。
                "pcf_url": (_pcfurl.official_pcf_url(code, pcf_szse_date) if _pcfurl else None),
                # 引擎产出
                "iopv": None, "iopv_prem": None, "nav": None, "nav_date": None,
                "nav_prem": None, "nav_src": None, "idx_chg": None,
                "iopv_err_bps": None, "suspend_weight": None, "err": None,
                # 结算汇兑四列（2026-09-18 加，东哥拍板）：
                #   settle_prem =「实际折溢价(T-1结算)%」= (1+实时折溢价)×中间价÷买入结算汇兑(T-1) − 1
                #   这是申购/赎回真实换汇口径的判据（换汇行按买卖结算汇兑比率成交，不按中间价）。
                "fx_mid": None, "settle_fx": None, "settle_date": None, "settle_prem": None,
                # 静态估值四列（仅 LOF 行出数，core/static_val 引擎）：
                #   static_val = nav(D-1)×(1+0.95×指数D涨幅)，与随后公布的 Nav(D) 核对答案；
                #   static_prem = 现价÷静态估值−1（东哥 LOF 程序同名列）；点击弹历史核对页。
                "idx_price": None, "static_val": None, "static_prem": None, "nav_chg_pct": None,
            }
            # 🔴 净值三列**先落地、与通道无关**：`nav`=**最新已公布净值**（腾讯 jj，
            #    与集思录同口径）、`nav_date`=其**净值日期**、`nav_prem`=现价对净值的
            #    溢价率（集思录同式 `现价/最新已公布净值−1`）。放在分流前 ⇒
            #    no_source/frozen/err 全部带着走。`nav_src`=取值来源（'jj'/'pcf'，
            #    **内部列、前端不渲染**）—— 回退到 'pcf' 时语义是基准日 D，别误读。
            _nav, _nav_d, _nav_src = nav_map.get(code, (None, None, None))
            row["nav"] = _nav
            row["nav_date"] = _nav_d
            row["nav_src"] = _nav_src
            _px0 = row["price"]
            if _px0 and row["nav"]:
                row["nav_prem"] = (_px0 / row["nav"] - 1) * 100

            # 🔴 F 商品期货（东哥 10-09 拍板，路 B）：161226 白银估值在 LOFarb（SSE 沪银 AG0
            #   推流 + 新浪 nf_AG0 双源，已完善），ETFarb 只做**引用**（lofarb_bridge），不重算。
            #   放在 woody 分支**之前**：161226 in_arbtest=1 本会走 woody continue（估值列全空），
            #   此处先接 bridge 把 LOFarb 的 rt_val/static_val/溢价搬进来。bridge 不可达/拿不到
            #   时（LOFarb 没起）降级回 woody 分支原逻辑（仍出门、估值空），不阻断看板。
            if (p.get("jsl_market") or "") == "F":
                try:
                    import lofarb_bridge as _lb
                except ImportError:
                    try:
                        from core import lofarb_bridge as _lb
                    except Exception:
                        _lb = None
                _ref = _lb.get_reference(code) if _lb else None
                if _ref:
                    row["lofarb"] = True
                    row["lofarb_source"] = _ref.get("source")
                    row["lofarb_category"] = _ref.get("category")
                    # 实时估值/溢价（盘中 AG0 非交易时段 = LOFarb 留空，前端显「—」，宁缺勿假）
                    row["iopv"] = _ref.get("rt_val")
                    row["iopv_prem"] = _ref.get("rt_premium")
                    # 静态估值（VWAP 口径，盘中恒有）+ 溢价
                    if _ref.get("static_val") is not None:
                        row["static_val"] = _ref["static_val"]
                        _px = row["price"]
                        if _px:
                            row["static_prem"] = (_px / _ref["static_val"] - 1) * 100
                    # 白银比价面板字段（AG0 昨结算价 + COMEX SI 交叉验证，前端「驱动列」用）
                    row["ag0_price"] = _ref.get("ag0_price")
                    row["ag0_settlement"] = _ref.get("ag0_settlement")
                    row["si_val"] = _ref.get("si_val")
                    row["si_premium"] = _ref.get("si_premium")
                    # 净值日期（LOFarb 口径 = 最新已公布净值日，与 ETFarb nav_date 同义）
                    if _ref.get("nav_date"):
                        row["nav_date"] = _ref["nav_date"]
                        if _ref.get("nav") is not None:
                            row["nav"] = _ref["nav"]
                    stat["lofarb_ref"] = stat.get("lofarb_ref", 0) + 1
                    rows.append(row)
                    continue
                # bridge 拿不到 ⇒ 落到下方 woody 分支（估值空出门），绝不兜底

            # 🔴 woody 标记（东哥 2026-09-21）：与 arbTest 重叠的基金，ETFarb 不再
            #   计算自算 IOPV / 实时估值 / 静态估值 / 结算溢价，仅保留官方 IOPV、
            #   净值、指数涨幅等自然字段；估值类列初始即 None，此处直接出门。
            #   前端名称列加「woody」徽标提示「数据以 arbTest 为准」。
            if p.get("in_arbtest"):
                row["woody"] = True
                # 指数涨幅自然保留（不依赖估值模型）：有 idx_code 即补取实时涨幅/点位
                if p.get("idx_code"):
                    _ix = self.idx_provider.get_change(p["idx_code"]) \
                        if getattr(self, "idx_provider", None) else None
                    if _ix:
                        row["idx_chg"] = _ix["pct"]
                        row["idx_price"] = _ix.get("price")
                        if not row.get("index_nm"):
                            row["index_nm"] = _ix.get("name")
                # 🔴 回退（东哥 2026-09-21）：woody 分支在下方 proxy 分支**之前** continue，
                #   而 E 市场腾讯源美股指数（NDX/SP500/DJI 等）本就**不登记 idx_code**
                #   （proxy 路径直接用 fund_iopv_config.index_key 解析）⇒ 这些基金落到
                #   本分支后「指数价 / 指数涨幅」两列全空（实测 41 只里仅道琼斯 1 只有值）。
                #   故 idx_code 取不到时，回退用 index_key 经 IndexProxyQuote 取数，
                #   让「究竟跟踪哪个指数」在本分支同样可见。取不到就留空显「—」，绝不兜底。
                if row.get("idx_price") is None:
                    try:
                        _c = IOPVCalculator(code, self.db, hk=self.hk)
                        _k = (_c.index_key or "").strip()
                        if _k:
                            _r = self.proxy.get_index_ratio(_k)
                            row["idx_price"] = _r.get("last")
                            row["idx_chg"] = ((_r["ratio"] - 1) * 100
                                              if _r.get("ratio") else None)
                            if not row.get("index_nm"):
                                row["index_nm"] = _r.get("name")
                    except Exception as _e:
                        row.setdefault("err", "")  # 指数缺源不是取数失败，不污染 err
                rows.append(row)
                continue

            # 🔴 LOF / 现金管理 估值分支（fund_type='LOF'）：不走 IOPV 模型
            #   估值 = NAV × (1 + POS × 指数涨跌幅%)。POS=0.95（东哥 2026-09-18 拍板：
            #   仓位统一 95%，即 arbTest unified_valuation.basket_valuation 矩阵式的
            #   单组件特例 `nav×(1+0.95×(指数比−1))`，fx=1——当日汇率漂移暂不建模）。
            #   与基金公司随后公布的 Nav 核对答案 =「静态估值」弹窗（core/static_val）。
            #   债券/主动型（val_method='bond'）：估值 = 净值（无指数可跟踪）。
            #   无免费指数源：估值留空显「—」，但净值/净值溢价照出。
            if (p.get("fund_type") or "ETF") == "LOF":
                row["quote_path"] = p.get("val_method")
                idx = (self.idx_provider.get_change(p.get("idx_code"))
                       if p.get("idx_code") else None)
                idx_pct = idx["pct"] if idx else None
                vm = (p.get("val_method") or "")
                if vm == "bond":
                    # 🔴 债券 ETF 日内估值（移植 arbTest BondETFValuation，东哥 09-18 授权）：
                    #   511880 日均增长法 / 511360 票息+国债指数000012 / 511520 票息+活跃10Y国债期货。
                    #   国债指数 000012 走 idx_provider（s_sh000012，已实测），活跃10Y国债期货走模块内新浪抓取（合约按交割月动态）。
                    #   est=日内预估净值（≠最新已公布净值），idx_chg=对应方向信号（511880 货币型留空）。
                    _tz = self.idx_provider.get_change("000012")
                    _tz_pct = _tz["pct"] if _tz else None
                    _tf_pct, _, _tf_px = get_treasury_futures_quote()
                    _b = estimate_bond_nav(code, _tz_pct, _tf_pct)
                    est = _b["est"] if _b["est"] is not None else _nav
                    row["idx_chg"] = _b["idx_chg"]
                    if _b.get("index_nm"):
                        row["index_nm"] = _b["index_nm"]
                    if _b.get("note"):
                        row["err"] = _b["note"]
                    row["bond_method"] = _b.get("method")
                    # 511990 等无历史库源的债券：estimate_bond_nav 已用腾讯jj实时净值兜底，
                    # 把 latest_nav/nav_date 回填到 nav 列，避免「净值日期」空白（非交易日也显实时日）。
                    if row.get("nav") is None and _b.get("latest_nav") is not None:
                        row["nav"] = _b["latest_nav"]
                        row["nav_date"] = _b.get("nav_date")
                        row["nav_src"] = "jj_realtime"
                    # 「指数价」列：511360=国债指数000012实时点位；511520=活跃10Y国债期货最新价
                    #   （动态合约，非硬编码T2609）。
                    #   🔴 旧逻辑用 `"T2609" in method` 判断，但 method 字段早已不含该字面量
                    #      （09-30 改动态换月，见 bond_etf_valuation），致 511520 永远退化成国债指数价
                    #      （与511360撞车，东哥 10-04 抓出）。改判 `_tf_px is not None`。
                    #   🔴🔴 东哥 10-04 再纠正：511520 跟踪的是 10Y 国债期货，国债指数是完全不同的标的，
                    #      绝不可在「取不到期货价」时退化显示国债指数价（会误导为同一标的）。
                    #      取不到期货价 ⇒ 显式标记 "失败"（前端红字），不做任何伪装。
                    if code == "511360":
                        row["idx_price"] = _tz.get("price") if _tz else None
                    elif code == "511520":
                        row["idx_price"] = _tf_px if _tf_px is not None else "失败"
                    # 静态估值（债券口径）：nav(D-1)+票息+方向(D)，回溯核对公布净值。
                    #   与 LOF 的 static_val 引擎同哲学，算法=本债券模型自身（详 bond 模块）。
                    try:
                        _bst = latest_static_bond(code, db=self.db)
                        if _bst:
                            row["static_val"] = _bst["static_val"]
                            _bpx = row["price"]
                            if _bpx and _bst["static_val"]:
                                row["static_prem"] = (_bpx / _bst["static_val"] - 1) * 100
                    except Exception:
                        pass
                else:
                    if idx is None:
                        est = None
                        row["err"] = "无免费指数源（P3 待接）"
                        row["idx_chg"] = None
                    else:
                        est = (_nav * (1 + 0.95 * idx_pct / 100.0)) if _nav is not None else None
                        row["idx_chg"] = idx_pct
                row["iopv"] = est                    # 复用 iopv 槽位存「估值」
                if est is not None:
                    stat["lof"] += 1
                    px = row["price"]
                    if px and est:
                        row["iopv_prem"] = (px / est - 1) * 100
                else:
                    stat["lof_missing"] = stat.get("lof_missing", 0) + 1
                # ---- 静态估值四列（core/static_val 引擎，数据已批量过期即补）----
                if idx is not None:
                    row["idx_price"] = idx.get("price")
                try:
                    _st = None if vm == "bond" else _sv.latest_static(code, self.db)
                    if _st:
                        row["static_val"] = _st["static_val"]
                        px = row["price"]
                        if px and _st["static_val"]:
                            row["static_prem"] = (px / _st["static_val"] - 1) * 100
                    row["nav_chg_pct"] = _sv.nav_change_pct(code, self.db)
                except Exception:
                    pass
                # 盘后冻结覆盖（与 #19 同口径）：IOPV 两列用快照，idx_chg 用实时值。
                # 🔴 债券 ETF（vm=='bond'）除外：国内品种、国债 15:15 已收，估值模型稳定，
                #   无「盘后跟美股漂移」问题；且快照可能由旧代码(est=净值)写入，覆盖会回退成净值。
                #   故债券始终用本轮实时估的值（est 已含方向），不被冻结快照覆盖。
                if frozen and vm != "bond":
                    s = snap_valid.get(code)
                    if s:
                        row["iopv"] = s.get("iopv")
                        row["iopv_prem"] = s.get("iopv_prem")
                    else:
                        row["iopv"] = None
                        row["iopv_prem"] = None
                rows.append(row)
                continue

            # ---- T-1 静态估值两列（2026-10-08 东哥拍板：E/A ETF 行与 LOF/黄金原油同构）----
            #   latest_static：159687 走 LCU 官方 NAV 桥接（σ 83bp→4.3bp），其余
            #   nav(D-1)×(1+0.95×指数D涨幅)。数据（净值/指数历史）由每日采集线程
            #   统一过期即补（backend.app._daily_collect_loop），缺数据的行留 None
            #   显「—」，宁空勿错。woody 行已在上方 continue（不出静态估值）。
            try:
                _st = _sv.latest_static(code, self.db)
                if _st:
                    row["static_val"] = _st["static_val"]
                    if row["price"] and _st["static_val"]:
                        row["static_prem"] = (row["price"] / _st["static_val"] - 1) * 100
            except Exception:
                pass

            try:
                # 🔴 沙特双雄（520830/159329）：100% 持港股 02830 ⇒ 实时估值直接用
                #   「02830 实时价 × HKD/CNY」驱动 = NAV(anchor) × driver(now)/driver(anchor)。
                #   绕开陈旧 PCF 篮子（09-17 快照 + 现金替代 ⇒ 自算 IOPV 失真 ~40bp）。
                #   02830 港股 16:00 收盘后其价即当日收盘，值稳定，故冻结逻辑无需覆盖。
                if code in _saudi.FUNDS:
                    _sr = _saudi.realtime_estimate(code, self.db)
                    if _sr and _sr.get("static_val"):
                        row["iopv"] = _sr["static_val"]
                        _px = row["price"]
                        if _px:
                            row["iopv_prem"] = (_px / _sr["static_val"] - 1) * 100
                        stat["perstock"] += 1
                        rows.append(row)
                        continue
                calc = IOPVCalculator(code, self.db, hk=self.hk)
                if path == "perstock":
                    if fx_err:
                        raise IOPVDataMissing(fx_err)
                    # 🔴 单券 FoF「报价冻结」闸门（2026-09-17 立，docs/40 §6.5）
                    #    低覆盖标的（成分 < 5）的篮子往往**只有一只 ETF**；若该 ETF
                    #    **今日零成交**，其「现价」只是昨收，与官方口径不可比 ——
                    #    实证：`2830` 南方沙特 / `3167` 工银南方中国 今日今开=最高=最低
                    #    =0、量额均为 0，用其昨收算出的 IOPV 偏离官方 **+45bp / −72bp**；
                    #    而今日成交 72.2 亿的 `3033`（513130 唯一成分）误差仅 **+1.5bp**。
                    #    ⇒ 宁可**不出数**（前端显「—」），也不给一个口径不清的数字。
                    #    ⚠️ 只在 low_cov 上闸；正常标的成分多，个别券零成交无妨。
                    if p["low_cov"]:
                        frz = [c for c in comps_map.get(code, [])
                               if c in marks_all and marks_all[c][2] == 0 and marks_all[c][3] == 0]
                        if frz:
                            stat["frozen"] += 1
                            row["err"] = ("成分 %s 今日零成交（报价冻结在昨收），"
                                          "与官方口径不可比" % "/".join(frz[:5]))
                            rows.append(row)
                            continue
                    res = calc.calc_realtime(
                        marks={c: (marks_all[c][0], marks_all[c][1]) for c in comps_map.get(code, [])
                               if c in marks_all} or None,
                        fx_info=fx_hkd,
                        market_price=row["price"] if row["price"] is not None else None,
                        d_close=subst_d.get(code))
                    stat["perstock"] += 1
                    row["suspend_weight"] = res.get("suspended_weight")
                else:
                    idx_key = (calc.index_key or "").strip()
                    if not idx_key:
                        # 🔴 无免费指数源（全池 **18 只** = A 2 / E 16，2026-09-17 实测
                        #    东财/腾讯均无：TOPIX / 中韩半导体 / 新交所新兴亚洲 / 全球中国教育 /
                        #    标普生物科技·油气·消费 / 纳指生物科技·科技 / 中概互联 30·50 /
                        #    东南亚科技 / 亚太精选 / 美国50 …）⇒
                        #    **IOPV 不出数**，前端显「—」。不计入 err —— 这是**已知缺源**，
                        #    不是取数失败，混进 err 会让「失败数」失去诊断意义。
                        #    ⚠️ 只是 IOPV 缺源：**净值/净值溢价率照出**（通道无关，见 `_nav_map`）。
                        stat["no_source"] += 1
                        row["err"] = "无免费指数源（P3 待接）"
                        rows.append(row)
                        continue
                    res = calc.calc_realtime_index_proxy(
                        market_price=row["price"] if row["price"] is not None else None)
                    stat["proxy"] += 1
                    row["idx_chg"] = (res["index_ratio"] - 1) * 100 if res.get("index_ratio") else None
                    # 🔴 指数价 / 指数名称（东哥 2026-09-21：E/A 也要像国内LOF 那样能看
                    #   清究竟跟踪哪个指数）。proxy 结果自带 index_last / index_name，
                    #   逐券路径由下方「指数涨幅统一补算」块补 idx_price；此处只在 proxy 路径设。
                    if res.get("index_last") is not None:
                        row["idx_price"] = res["index_last"]
                    if not row.get("index_nm") and res.get("index_name"):
                        row["index_nm"] = res["index_name"]
                row["iopv"] = res["iopv"]
                px = row["price"]
                if px and row["iopv"]:
                    row["iopv_prem"] = (px / row["iopv"] - 1) * 100
                if row["official_iopv"]:
                    row["iopv_err_bps"] = (row["iopv"] / row["official_iopv"] - 1) * 10000
                    stat["official_cmp"] += 1
                    stat["err_bps"].append(row["iopv_err_bps"])
            except Exception as e:
                stat["err"] += 1
                row["err"] = str(e)[:180]
                errors.append({"code": code, "err": row["err"]})
            # 🔴 指数涨幅统一补算（亚洲TAB 多数为 ETF，走 perstock 路径时 idx_chg 未设
            #   ⇒ 「指数涨幅」列空白。凡有 idx_code 且非债券型，统一取东财 secid 通道。
            #   LOF 分支 / proxy 路径已设过的不会重复覆盖（idx_chg 非 None 则跳过）。
            if row.get("idx_chg") is None and p.get("idx_code"):
                _is_bond = ((p.get("fund_type") or "ETF") == "LOF"
                            and (p.get("val_method") or "") == "bond")
                if not _is_bond:
                    _idx = self.idx_provider.get_change(p["idx_code"])
                    if _idx:
                        row["idx_chg"] = _idx["pct"]
                        row["idx_price"] = _idx.get("price")
                        if not row.get("index_nm"):
                            row["index_nm"] = _idx.get("name")
            # 🔴 #19 盘后冻结：冻结时 IOPV 两列用盘中快照覆盖实时值（官方 IOPV 盘后冻死）。
            #   ⚠️ idx_chg（指数涨幅）**不覆盖**——指数本身有当日收盘，与市价冻结无关，
            #   应永远用实时值（上面统一补算块已设）；否则周末/盘后会被旧快照的 None 抹掉。
            if frozen:
                s = snap_valid.get(code)
                if s:
                    row["iopv"] = s.get("iopv")
                    row["iopv_prem"] = s.get("iopv_prem")
                    if row["official_iopv"] and row["iopv"]:
                        row["iopv_err_bps"] = (row["iopv"] / row["official_iopv"] - 1) * 10000
                else:
                    # 无当日快照 ⇒ 留空显「—」，绝不给盘后失真值
                    row["iopv"] = None
                    row["iopv_prem"] = None
            rows.append(row)
        # ---- #19：非冻结时把本轮三列结果**合并**进快照（供盘后冻结读取） ----
        #   🔴 必须合并、不能整文件覆盖（2026-09-18 修）：预热线程轮着建 A/E/L/C，
        #      整文件覆盖会让「最后写的那一轮」清掉其它市场全部快照 ⇒ 盘后冻结时
        #      其它市场无快照、三列全空（实测：14:55 L 的 41 只覆盖了 A 的 219 只）。
        #   顺带清掉超过 24h 的陈旧条目，文件不无限膨胀。
        if not frozen:
            now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            now0 = datetime.datetime.now()
            snap = {c: v for c, v in self._load_freeze_snap().items()
                    if self._snap_fresh(v, now0)}
            for r in rows:
                snap[r["code"]] = {"idx_chg": r["idx_chg"], "iopv": r["iopv"],
                                   "iopv_prem": r["iopv_prem"], "ts": now_iso}
            self._save_freeze_snap(snap)
        # ---- 结算汇兑四列（2026-09-18 加，东哥拍板：亚洲/欧美列表上「实际折溢价(T-1结算)%」）----
        #   公式（与 backend.app._settle_premium_bps 同源）：实际折溢价 = (1+实时折溢价)×中间价÷买入结算汇兑(T-1) − 1
        #   🔴 放在冻结覆盖**之后**：冻结行展示的是盘中快照溢价，结算口径必须与展示口径同源。
        #   🔴 只对 ETF 行生效：LOF 申购是现金申购（无中登换汇环节），套用会造假判据。
        #   美元/日元/欧元等 QDII 无公开结算汇兑比率源（中登只公布港股通 HKD）⇒ 显「—」。
        cfg_map = self._cfg_map(codes)
        pairs = sorted({fx for fx, _ in cfg_map.values() if fx})
        mid_map = {}
        for pair in pairs:
            try:
                mid_map[pair] = (self._fx_spot(pair) or {}).get("rate")
            except Exception:
                mid_map[pair] = None
        settle_keys = []
        for fx, src in cfg_map.values():
            if fx:
                mk = "SZ" if str(src or "").lower() == "szse_txt" else "SH"
                settle_keys += ["%s_SETTLE_BUY_%s" % (fx, mk), "%s_SETTLE_BUY" % fx]
        settle_map = self._settle_map(settle_keys)
        for r in rows:
            if (r.get("fund_type") or "ETF") != "ETF":
                continue                              # LOF 现金申购，无中登换汇环节
            fx, src = cfg_map.get(r["code"], (None, None))
            if not fx or r.get("iopv_prem") is None:
                continue
            mid = mid_map.get(fx)
            mk = "SZ" if str(src or "").lower() == "szse_txt" else "SH"
            st = settle_map.get("%s_SETTLE_BUY_%s" % (fx, mk)) or settle_map.get("%s_SETTLE_BUY" % fx)
            if mid and st:
                r["fx_mid"] = mid
                r["settle_fx"], r["settle_date"] = st
                r["settle_prem"] = ((1 + r["iopv_prem"] / 100.0) * mid / st[0] - 1) * 100
        if stat["err_bps"]:
            s = sorted(stat["err_bps"])
            stat["err_bps_median"] = s[len(s) // 2]
            stat["err_bps_abs_p90"] = sorted(abs(x) for x in s)[int(len(s) * 0.9) - 1] if s else None
        stat.pop("err_bps", None)
        # 🔴 冻结模式下按**最终行**重算通道统计（2026-09-18 修）：
        #    上面的 perstock/proxy/lof 计数发生在冻结覆盖**之前**，快照缺失的行
        #    三列已被置空 ⇒ 若不重算，头部会显示「出数 213」而表格 IOPV 全空。
        if frozen:
            for k in ("perstock", "proxy", "lof", "no_source", "frozen", "err"):
                stat[k] = 0
            stat.pop("lof_missing", None)
            for r in rows:
                if r["iopv"] is not None:
                    if r["quote_path"] == "perstock":
                        stat["perstock"] += 1
                    elif r["quote_path"] == "proxy":
                        stat["proxy"] += 1
                    else:
                        stat["lof"] += 1
                elif r.get("err") == "无免费指数源（P3 待接）":
                    stat["no_source"] += 1
                elif r.get("err"):
                    stat["err"] += 1
                else:
                    stat["frozen"] += 1
        return {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "market": market, "count": len(rows), "rows": rows,
                "stats": stat, "errors": errors}


def main():
    import argparse
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description="跨境 ETF 看板 —— 行情编排自测")
    ap.add_argument("--market", choices=["A", "E"], help="只取一个市场")
    ap.add_argument("--limit", type=int, help="只取前 N 只（按份额降序）")
    ap.add_argument("--min-share", type=float, help="份额门槛（万份）")
    a = ap.parse_args()
    b = ETFMarketBoard()
    r = b.build(market=a.market, limit=a.limit, min_share=a.min_share, force_live=True)
    print("时间 %s  共 %d 只" % (r["ts"], r["count"]))
    print("统计：", json.dumps(r["stats"], ensure_ascii=False))
    print()
    print("%-7s %-18s %6s %8s %8s %8s %8s %9s %9s %8s" %
          ("code", "name", "path", "price", "iopv", "prem%", "nav", "navprem%", "官方IOPV", "误差bp"))
    for x in r["rows"][:30]:
        print("%-7s %-18s %6s %8s %8s %8s %8s %9s %9s %8s" % (
            x["code"], (x["name"] or "")[:18], (x["quote_path"] or "-")[:6],
            _f(x["price"]), _f(x["iopv"]), _f(x["iopv_prem"]),
            _f(x["nav"]), _f(x["nav_prem"]), _f(x["official_iopv"]), _f(x["iopv_err_bps"])))
    if r["errors"]:
        print()
        print("=== 取数失败 %d 只 ===" % len(r["errors"]))
        for e in r["errors"][:15]:
            print("  %s  %s" % (e["code"], e["err"]))
    nod = [x for x in r["rows"] if x["iopv"] is None and x.get("err")
           and x["code"] not in {e["code"] for e in r["errors"]}]
    if nod:
        print()
        print("=== 未出数（已知缺源 / 报价冻结）%d 只 ===" % len(nod))
        for x in nod[:15]:
            print("  %s  %s" % (x["code"], x["err"]))


def _f(v, n=4):
    return ("%." + str(n) + "f") % v if isinstance(v, (int, float)) else "—"


if __name__ == "__main__":
    main()
