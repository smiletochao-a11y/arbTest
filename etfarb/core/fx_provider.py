#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/fx_provider.py — P2: 汇率 Provider（per-ETF 币种，513590 先跑通一条）
=====================================================================
IOPV 引擎的汇率口径核心。设计原则（第一性原理，不过度设计）：

  权威自动源：国家外汇管理局中间价（ChinaMoney ccpr.json）—— 已验证可用
              （proxies={} 禁用代理 + verify=False，参考 LOF 套利主程序
               data_fetcher.fetch_official_exchange_rate）。写入 fx_rate 表，
               按 lastDate 日期 UPSERT。
  用户 Excel ：Excel/CSV 灌入 fx_rate 表（INSERT OR REPLACE），**优先于自动抓**
              —— 东哥手工核对的中间价最可信，自动抓不覆盖手填同日值。
  实时市场汇率：新浪 `hq.sinajs.cn/list=fx_shkdcny`（需带 Referer，返回 GBK）
              —— **2026-09-17 重新实测可用**（旧注释「新浪已失效」作废：那是 9-14
                 用错入口的结论）。逐券 IOPV 用它，见 `get_spot_rate_info()`。

  ⚠️ 红线（东哥 2026-09-14 明确）：**绝不兜底**。
     取不到中间价一律抛 FXMissingError，绝不用 config.fx_rate 等默认值静默算错。
     fund_iopv_config.fx_rate 列仅作参考展示，永不进计算。

用法：
  python fx_provider.py                          # 建表 + seed历史 + 抓今日ChinaMoney + 展示
  python fx_provider.py --seed                  # 仅 seed 9-10/9-11 HKD/CNY
  python fx_provider.py --fetch-chinamoney      # 仅抓今日中间价写入 fx_rate
  python fx_provider.py --csv path.csv          # 从用户 Excel 导出的 CSV 灌入每日中间价(优先)
  python fx_provider.py --fetch-settle          # 抓「最新一期结算汇兑比率」(深API + 沪JS)
  python fx_provider.py --settle-history 20250414 20260916   # 反推回填历史结算汇率(中值)
"""
import os
import re
import ssl
import csv
import sqlite3
import datetime
import importlib.util
import urllib.request
import argparse

import requests
requests.packages.urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import db as _db   # noqa: E402  core/db.py —— 单库方案 arb_master.db
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

# 国家外汇管理局人民币汇率中间价（每日 9:15 公布，返回最新一期）
CHINAMONEY_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
CHINAMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
}

# ccy_pair -> 在 chinamoney records 里 vrtName 的匹配关键字
CCY_NAME_KEYS = {
    "HKD/CNY": ("港", "HKD", "HK"),
    "USD/CNY": ("美元", "USD", "US"),
    "JPY/CNY": ("日元", "日圆", "JPY"),
}

# ----------------------------------------------------------------------
# 结算汇兑比率（港股通申赎换汇实际用的汇率）—— 2026-09-16 打通
# ----------------------------------------------------------------------
# 🔴 三个口子（全部免费无鉴权；静态抓不到，须带 Referer；js 是页面同源数据文件）：
#   沪深「结算汇兑比率」最新一期 → 给的就是 **T-1**（T 日终净额换汇尚未完成）：
#     深  https://www.szse.cn/api/report/index/sgtbalancerate
#     沪  https://www.sse.com.cn/services/hkexsc/home/ratios/data_1136.js
#   沪深「参考汇率」最新一期 = 中值×(1∓3%)（固定 3% 带，**只用于预冻结**）：
#     深  https://www.szse.cn/api/report/index/sgtreferrate
#     沪  https://www.sse.com.cn/services/hkexsc/home/exchange/data_1134.js
#   沪「参考汇率」**历史区间**（历史唯一口，2025-04 起）：
#     query.sse.com.cn/commonSoaQuery.do?sqlId=FW_HGT_GGTHL&updateDate=..&updateDateEnd=..
SZSE_SETTLE_API = "https://www.szse.cn/api/report/index/sgtbalancerate"
SZSE_REFER_API = "https://www.szse.cn/api/report/index/sgtreferrate"
SSE_SETTLE_JS = "https://www.sse.com.cn/services/hkexsc/home/ratios/data_1136.js"
SSE_REFER_JS = "https://www.sse.com.cn/services/hkexsc/home/exchange/data_1134.js"
SSE_REFER_HIST_API = ("https://query.sse.com.cn/commonSoaQuery.do?isPagination=true"
                      "&updateDate={d1}&updateDateEnd={d2}&sqlId=FW_HGT_GGTHL"
                      "&pageHelp.cacheSize=1&pageHelp.pageSize={size}"
                      "&pageHelp.pageNo={page}&pageHelp.beginPage={page}&pageHelp.endPage={page}")
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------
# 🔴 实时**市场汇率**（新浪 hq.sinajs.cn）—— 逐券 IOPV 专用（2026-09-17 立）
# ----------------------------------------------------------------------
# 与 chinamoney **中间价**是**两种口径**，差别是结构性的（docs/21 §5.2 实测钉死）：
#   · 官方实时 IOPV 用的就是**市场汇率** —— 三只纯港股 ETF 反推隐含 0.85509，
#     与新浪 `fx_shkdcny` 现价 0.855183 吻合（差 0.9bp）。
#   · chinamoney 中间价按 **USD/HKD = 7.80** 折算 ⇒ HKD/CNY 偏高约 **+0.81%**
#     （DB `HKD/CNY` = 0.8621 vs 市场 0.8551）。
#   ⇒ 逐券路径若用中间价：IOPV 系统性偏低 ⇒ **溢价虚高 +81bp**
#     （513590 实测 +82.0bp、159366 +83.4bp）。
# 取用规则：**逐券用本通道**；中间价只用于官方净值折算与篮子校准（那里的口径本就是中间价）。
SINA_FX_CODES = {
    "HKD/CNY": "fx_shkdcny",
    "USD/CNY": "fx_susdcny",
    "JPY/CNY": "fx_sjpycny",
    "EUR/CNY": "fx_seurcny",
    "GBP/CNY": "fx_sgbpcny",
}
SINA_FX_URL = "https://hq.sinajs.cn/list="


class FXMissingError(Exception):
    """FX 中间价缺失时抛出。绝不兜底静默计算，必须暴露错误让上游处理。"""
    pass


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------
def init_db(db=DB_PATH):
    c = sqlite3.connect(db)
    c.execute("""
    CREATE TABLE IF NOT EXISTS fx_rate(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ccy_pair TEXT NOT NULL,
        rate_date TEXT NOT NULL,
        rate REAL NOT NULL,
        source TEXT,
        fetched_at TEXT,
        UNIQUE(ccy_pair, rate_date)
    )
    """)
    c.commit()
    c.close()
    return db


def _match_ccy(name, ccy_pair):
    keys = CCY_NAME_KEYS.get(ccy_pair, (ccy_pair.split("/")[0].upper(),))
    name_u = (name or "").upper()
    return any(k.upper() in name_u for k in keys)


# ----------------------------------------------------------------------
# FX Provider
# ----------------------------------------------------------------------
class FXProvider:
    def __init__(self, fund_code="513590", db=DB_PATH):
        self.db = db
        self.fund_code = fund_code
        cfg = self._read_config(fund_code)
        self.ccy_pair = (cfg["fx_currency"] if cfg else None) or "HKD/CNY"
        # fund_iopv_config.fx_rate 仅作参考展示，绝不进计算（东哥红线：无兜底、缺则暴露）
        self.config_fx_ref = cfg["fx_rate"] if cfg else None

    def _read_config(self, fund_code):
        c = sqlite3.connect(self.db)
        row = c.execute(
            "SELECT fx_currency, fx_rate FROM fund_iopv_config WHERE fund_code=?",
            (fund_code,)).fetchone()
        c.close()
        return {"fx_currency": row[0], "fx_rate": row[1]} if row else None

    def get_rate(self, date=None):
        """严格模式：取某日(或最新一期)中间价，float。

        找不到 → 抛 FXMissingError（绝不兜底）。
        """
        return self.get_rate_info(date)["rate"]

    def get_rate_info(self, date=None):
        """严格模式：返回 {rate, rate_date, source}。

        找不到 → 抛 FXMissingError（绝不兜底静默算错）。
        兼容 date 传 'YYYYMMDD' 或 'YYYY-MM-DD' 两种格式（自动归一化）。
        """
        if date and len(date) == 8 and date.isdigit():
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        c = sqlite3.connect(self.db)
        try:
            if date:
                row = c.execute(
                    "SELECT rate, source FROM fx_rate WHERE ccy_pair=? AND rate_date=?",
                    (self.ccy_pair, date)).fetchone()
                if row:
                    return {"rate": float(row[0]), "rate_date": date, "source": row[1]}
            else:
                row = c.execute(
                    "SELECT rate, rate_date, source FROM fx_rate WHERE ccy_pair=? "
                    "ORDER BY rate_date DESC LIMIT 1",
                    (self.ccy_pair,)).fetchone()
                if row:
                    return {"rate": float(row[0]), "rate_date": row[1], "source": row[2]}
        finally:
            c.close()
        if date:
            raise FXMissingError(
                f"[FX缺失] {self.ccy_pair} {date} 在 fx_rate 表无中间价记录。"
                f"请先运行 fx_provider.py 抓 ChinaMoney 或导入用户 CSV，不要靠兜底值算错。")
        raise FXMissingError(
            f"[FX缺失] {self.ccy_pair} 在 fx_rate 表无任何记录。"
            f"请先运行 fx_provider.py 抓 ChinaMoney 或导入用户 CSV，不要靠兜底值算错。")

    def get_spot_rate_info(self, pair=None):
        """🔴 实时**市场汇率**（新浪）—— 逐券 IOPV 专用，见 docs/21 §5.2。

        与 `get_rate_info()`（chinamoney **中间价**）的区别是**结构性的**：
        中间价按 USD/HKD=7.80 折算 ⇒ HKD/CNY 偏高约 **+0.81%**（≈ +81bp），
        而官方实时 IOPV 用的就是市场汇率（实测隐含 0.85509 = 新浪现价 0.855183）。

        ⇒ **逐券路径用本方法**；中间价只用于官方净值折算 / 篮子校准。
        取不到 → 抛 `FXMissingError`（**绝不退回中间价兜底**，那等于把 81bp 误差藏起来）。

        返回 `{rate, prev, rate_date, source, code}`。"""
        pair = pair or self.ccy_pair
        code = SINA_FX_CODES.get(pair)
        if not code:
            raise FXMissingError(
                "[FX缺失] %s 无实时市场汇率通道（已登记：%s）。"
                "如需新币种，请先在本模块 SINA_FX_CODES 登记并实测。"
                % (pair, sorted(SINA_FX_CODES)))
        try:
            r = requests.get(SINA_FX_URL + code, timeout=15, verify=False, headers={
                "User-Agent": _BROWSER_UA, "Referer": "https://finance.sina.com.cn/"})
            raw = r.content.decode("gbk", "replace")
        except Exception as e:
            raise FXMissingError("[FX缺失] 新浪 %s 请求失败: %s" % (code, e))
        m = re.search(r'="([^"]*)"', raw)
        if not m or not m.group(1).strip():
            raise FXMissingError("[FX缺失] 新浪 %s 无返回（空响应）" % code)
        parts = m.group(1).split(",")
        if len(parts) < 4:
            raise FXMissingError("[FX缺失] 新浪 %s 字段不足: %r" % (code, parts[:6]))
        try:
            last, prev = float(parts[1]), float(parts[3])
        except ValueError:
            raise FXMissingError("[FX缺失] 新浪 %s 数值解析失败: %r" % (code, parts[:5]))
        if last <= 0:
            raise FXMissingError("[FX缺失] 新浪 %s 现价非法: %r" % (code, last))
        return {"rate": last, "prev": prev,
                "rate_date": datetime.date.today().strftime("%Y-%m-%d"),
                "source": "sina_spot", "code": code}

    def seed_rates(self, db=DB_PATH):
        """把已验证脚本里的 9-10/9-11 HKD/CNY 中间价灌入 fx_rate 表。"""
        try:
            vp = os.path.join(HERE, "verify_iopv_513590_pcf.py")
            spec = importlib.util.spec_from_file_location("verify_mod", vp)
            vm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vm)
            RATE = vm.RATE  # {"0910":0.86413, "0911":0.86384}
        except Exception as e:
            print("seed 跳过（无法加载 verify 脚本）:", e)
            return 0
        c = sqlite3.connect(db)
        n = 0
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for k, v in RATE.items():
            d = "2026-09-" + k[2:4]   # "0910" -> "2026-09-10"
            # INSERT OR IGNORE：不覆盖 ChinaMoney 自动抓到的同日值（若已存在）
            c.execute(
                "INSERT OR IGNORE INTO fx_rate(ccy_pair,rate_date,rate,source,fetched_at) "
                "VALUES(?,?,?,?,?)",
                (self.ccy_pair, d, v, "seed_verify", now))
            n += 1
        c.commit()
        c.close()
        return n

    def update_from_csv(self, path, db=DB_PATH):
        """从用户 Excel 导出的 CSV（date,rate 两列）灌入每日中间价。

        用 INSERT OR REPLACE —— 用户手工核对值**优先于**自动抓，可覆盖同日 ChinaMoney 值。
        """
        c = sqlite3.connect(db)
        n = 0
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                d, v = row[0].strip(), row[1].strip()
                if not v:
                    continue
                try:
                    rate = float(v)
                except ValueError:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO fx_rate(ccy_pair,rate_date,rate,source,fetched_at) "
                    "VALUES(?,?,?,?,?)",
                    (self.ccy_pair, d, rate, "user_csv", now))
                n += 1
        c.commit()
        c.close()
        return n

    def fetch_chinamoney(self, ccy_pair=None, db=DB_PATH):
        """从国家外汇管理局(ChinaMoney)抓最新一期中间价，写入 fx_rate 表。

        返回 (rate, rate_date) 或 None。这是 FX 的权威自动源。
        注意：接口只给最新一期(lastDate)，历史某日需从 Excel/seed 另行提供。
        proxies={} 禁用代理（防被本机/沙箱代理重定向），verify=False 忽略证书。
        """
        pair = ccy_pair or self.ccy_pair
        try:
            r = requests.get(CHINAMONEY_URL, headers=CHINAMONEY_HEADERS,
                             proxies={}, timeout=30, verify=False)
            if r.status_code != 200:
                print(f"  [chinamoney] HTTP {r.status_code}")
                return None
            d = r.json()
            last_date = (d.get("data") or {}).get("lastDate")
            recs = d.get("records") or (d.get("data") or {}).get("records") or []
            rate = None
            matched_name = None
            for rec in recs:
                nm = rec.get("vrtName", "") or ""
                if _match_ccy(nm, pair):
                    # 🔴 单位必须还原：ChinaMoney 对日元等按「100 外币 / 人民币」报价
                    #    （vrtName='100日元/人民币' → price=4.3419 ⇒ JPY/CNY = 0.043419）。
                    #    港币是 1:1（'港元/人民币'，无数字前缀）。漏掉这步 JPY 会被放大 100 倍。
                    head = ""
                    for ch in nm.strip():
                        if ch.isdigit():
                            head += ch
                        else:
                            break
                    scale = int(head) if head else 1
                    rate = float(rec.get("price")) / scale
                    matched_name = nm
                    break
            if rate is None:
                print(f"  [chinamoney] 未找到 {pair} 条目 (共 {len(recs)} 条)")
                return None
            # last_date 形如 "2026-09-14 9:15" -> 取日期部分
            rate_date = (last_date.split(" ")[0] if last_date
                         else datetime.date.today().strftime("%Y-%m-%d"))
            c = sqlite3.connect(db)
            # INSERT OR IGNORE：不覆盖用户手填/已存在的同日值（用户优先）
            c.execute(
                "INSERT OR IGNORE INTO fx_rate(ccy_pair,rate_date,rate,source,fetched_at) "
                "VALUES(?,?,?,?,?)",
                (pair, rate_date, rate, "chinamoney",
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            c.commit()
            c.close()
            return rate, rate_date
        except Exception as e:
            print("  [chinamoney] 抓取失败:", e)
            return None

    # ------------------------------------------------------------------
    # 结算汇兑比率：自动抓取 + 历史反推（2026-09-16）
    # ------------------------------------------------------------------
    def settle_key(self, kind, market=""):
        """结算汇兑比率的 fx_rate 键位。

        `kind` 取 'BUY'（赎回换汇：基金卖港股→买入港币）/ 'SELL'（申购换汇）。
        `market` 取 'SZ' / 'SH' / ''：**沪深两市的结算汇兑比率是两套报价**
        （实测 9-15 深 0.85519/0.85521 vs 沪 0.85518/0.85522，中值相同、两脚差 ~1e-5）。
        不传 market 即老键位（历史手填值都落在这里），保持向后兼容。"""
        return "%s_SETTLE_%s%s" % (self.ccy_pair, kind.upper(), ("_" + market.upper()) if market else "")

    def _put_settle(self, key, date, rate, source, db=DB_PATH):
        """写一条结算汇率。**用户手填/CSV 值永不覆盖**；自动源之间后写覆盖先写。"""
        c = sqlite3.connect(db)
        row = c.execute("SELECT source FROM fx_rate WHERE ccy_pair=? AND rate_date=?",
                        (key, date)).fetchone()
        if row and str(row[0] or "").startswith("user_"):
            c.close()
            return "kept"
        c.execute("INSERT OR REPLACE INTO fx_rate(ccy_pair,rate_date,rate,source,fetched_at) "
                  "VALUES(?,?,?,?,?)",
                  (key, date, rate, source,
                   datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
        c.close()
        return "written"

    @staticmethod
    def _pick(d, *keys):
        """在 dict/JSON 里按候选键名容错取值（沪 js 的变量名带 `_clear` 后缀）。"""
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return None

    def fetch_settle_latest(self, db=DB_PATH):
        """抓**最新一期**「结算汇兑比率」（买入/卖出）并落库，两市各写各的键。

        源：深交所 `sgtbalancerate`（JSON，applyDate/buyRate/sellRate）
            上交所 `data_1136.js`（VALID_DATE_clear / BUY_PRICE_clear / SELL_PRICE_clear）
        ⚠️ 两家给的日期**本来就是 T-1**（T 日终净额换汇尚未完成）—— 与主看板
           「实际折溢价(T-1结算)」口径天然吻合；历史页要的是 D 当天的真值，
           盘后重跑本函数即可补上。
        返回 [(market, date, buy, sell)]；抓不到返回 []（不兜底）。"""
        out = []
        hdr = {"User-Agent": _BROWSER_UA}
        # 深交所（注意 result 是**两个元素**：各放 buy / sell 半边）
        try:
            r = requests.get(SZSE_SETTLE_API, headers=dict(hdr, Referer="https://www.szse.cn/szhk/index.html"),
                             proxies={}, timeout=20, verify=False)
            j = r.json()
            d = str(j.get("applyDate") or "").strip()
            flat = {}
            for it in (j.get("result") or []):
                if isinstance(it, dict):
                    flat.update(it)
            buy = self._pick(flat, "buyRate", "buyRateBar")
            sell = self._pick(flat, "sellRate", "sellRateBar")
            if d and buy and sell:
                self._put_settle(self.settle_key("BUY", "SZ"), d, float(buy), "szse_api", db)
                self._put_settle(self.settle_key("SELL", "SZ"), d, float(sell), "szse_api", db)
                out.append(("SZ", d, float(buy), float(sell)))
        except Exception as e:
            print("  [settle] 深交所抓取失败:", str(e)[:120])
        # 上交所
        try:
            r = requests.get(SSE_SETTLE_JS, headers=dict(hdr, Referer="https://www.sse.com.cn/services/hkexsc/home/"),
                             proxies={}, timeout=20, verify=False)
            txt = r.text
            def v(name):
                m = re.search(name + r"\s*=\s*'([^']*)'", txt)
                return m.group(1).strip() if m else None
            d = v("VALID_DATE_clear")
            buy, sell = v("BUY_PRICE_clear"), v("SELL_PRICE_clear")
            if d and buy and sell:
                self._put_settle(self.settle_key("BUY", "SH"), d, float(buy), "sse_js", db)
                self._put_settle(self.settle_key("SELL", "SH"), d, float(sell), "sse_js", db)
                out.append(("SH", d, float(buy), float(sell)))
        except Exception as e:
            print("  [settle] 上交所抓取失败:", str(e)[:120])
        return out

    def fetch_settle_history(self, d1, d2, db=DB_PATH, market="SH"):
        """用「参考汇率」历史**反推结算汇兑比率中值**，回填 [d1, d2]（YYYYMMDD）。

        🔴 恒等式（2026-09-16 实测 9-07~9-15 七天误差 **0.00000**）：
              结算汇兑比率中值 = (参考汇率买入价 + 参考汇率卖出价) ÷ 2
           参考汇率 = 中值 ×(1∓3%)（固定 3% 带，只用于预冻结）；结算汇兑比率 = 中值 ±≈0.2bp。

        ⇒ 历史某日的**买入/卖出真实两脚**无处可取（官方只发最新一期），但**中值可精确反推**；
          两脚与中值的差 ≤3e-5（≈0.03bp），远低于折溢价噪声底（159366 实测 σ≈48bp）。
          故历史日按中值落库、`source='sse_referrate_mid'` 留痕；
          一旦某日拿到官方真值，`fetch_settle_latest` 会把它覆盖掉（用户手填值始终优先）。

        ⚠️ 沪深两市参考汇率**中值完全相同**（实测逐日一致），故历史回填两市同值。
        返回写入/保留计数 dict。"""
        hdr = {"User-Agent": _BROWSER_UA,
               "Referer": "https://www.sse.com.cn/services/hkexsc/disclo/ratios/"}
        stat = {"written": 0, "kept": 0, "rows": 0}
        page, size = 1, 200
        while True:
            url = SSE_REFER_HIST_API.format(d1=d1, d2=d2, size=size, page=page)
            try:
                r = requests.get(url, headers=hdr, proxies={}, timeout=30, verify=False)
                j = r.json()
            except Exception as e:
                print("  [settle-hist] 第 %d 页失败: %s" % (page, str(e)[:120]))
                break
            rows = j.get("result") or []
            if not rows:
                break
            for it in rows:
                d = self._pick(it, "validDate", "VALID_DATE", "tradeDate", "date")
                b = self._pick(it, "buyPrice", "BUY_PRICE", "buyRate")
                s = self._pick(it, "sellPrice", "SELL_PRICE", "sellRate")
                if not (d and b and s):
                    continue
                mid = (float(b) + float(s)) / 2.0
                iso = "%s-%s-%s" % (str(d)[:4], str(d)[4:6], str(d)[6:8])
                for kind in ("BUY", "SELL"):
                    r1 = self._put_settle(self.settle_key(kind, market), iso, mid,
                                          "sse_referrate_mid", db)
                    stat["written" if r1 == "written" else "kept"] += 1
                stat["rows"] += 1
            if len(rows) < size:
                break
            page += 1
        return stat

    def sanity_check(self):
        """抓 ChinaMoney 最新一期，与 fx_rate 表已有同日记比较偏离。不兜底。

        仅作对账告警，不影响计算；计算口径永远以 fx_rate 表为准（无则报错）。
        """
        res = self.fetch_chinamoney(self.ccy_pair)
        if not res:
            return {"ok": False, "reason": "chinamoney 抓取失败"}
        latest, rate_date = res
        c = sqlite3.connect(self.db)
        row = c.execute(
            "SELECT rate, source FROM fx_rate WHERE ccy_pair=? AND rate_date=?",
            (self.ccy_pair, rate_date)).fetchone()
        c.close()
        if row:
            dev = (latest - row[0]) / row[0] * 100
            return {"ok": True, "chinamoney": latest, "rate_date": rate_date,
                    "db_rate": row[0], "db_source": row[1], "dev_pct": dev}
        return {"ok": True, "chinamoney": latest, "rate_date": rate_date,
                "db_rate": None, "db_source": None}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="P2: 汇率 Provider（per-ETF 币种，ChinaMoney 中间价）")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--fetch-chinamoney", action="store_true")
    ap.add_argument("--csv", type=str, default=None)
    ap.add_argument("--settle-buy", type=float, default=None,
                    help="手动喂入「买入结算汇兑比率」。存入 fx_rate 的 {fx_currency}_SETTLE_BUY 键（赎回换汇）")
    ap.add_argument("--settle-sell", type=float, default=None,
                    help="手动喂入「卖出结算汇兑比率」。存入 fx_rate 的 {fx_currency}_SETTLE_SELL 键（申购换汇）")
    ap.add_argument("--date", type=str, default=None,
                    help="结算比率的**适用日期** YYYY-MM-DD（与 --settle-buy/--settle-sell 配合；缺省=今天）")
    ap.add_argument("--fetch-settle", action="store_true",
                    help="自动抓「最新一期结算汇兑比率」（深交所 API + 上交所 js），两市各写各的键")
    ap.add_argument("--settle-history", nargs=2, metavar=("D1", "D2"), default=None,
                    help="用上交所参考汇率历史反推结算汇兑比率中值，回填区间 YYYYMMDD YYYYMMDD")
    ap.add_argument("--market", choices=["SH", "SZ"], default="SH",
                    help="配合 --settle-history：写哪一套键位（默认 SH）。"
                         "🔴 深市标的（pcf_source=szse_txt，如 159366）**必须传 SZ** —— "
                         "它读的是 {ccy}_SETTLE_*_SZ，写成 _SH 会取不到且**不报错**")
    args = ap.parse_args()

    init_db()

    if args.settle_history:
        fx = FXProvider("513590")
        st = fx.fetch_settle_history(args.settle_history[0], args.settle_history[1],
                                     market=args.market)
        print(">>> 结算汇率历史回填(参考汇率中值, 键位后缀 _%s): %s" % (args.market, st))
        print("    写入键: %s / %s"
              % (fx.settle_key("BUY", args.market), fx.settle_key("SELL", args.market)))
        return

    if args.fetch_settle:
        fx = FXProvider("513590")
        res = fx.fetch_settle_latest()
        if not res:
            print(">>> 结算汇兑比率自动抓取失败（不兜底）")
        for mk, d, b, s in res:
            print(">>> 结算汇兑比率[%s] %s  买入 %s / 卖出 %s  中值 %.5f"
                  % (mk, d, b, s, (b + s) / 2))
        return

    if args.csv:
        n = FXProvider("513590").update_from_csv(args.csv)
        print(f">>> 从 CSV 灌入每日中间价(用户优先): {n} 条")
        return

    if args.settle_buy is not None or args.settle_sell is not None:
        fx = FXProvider("513590")
        d = args.date or datetime.date.today().strftime("%Y-%m-%d")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c = sqlite3.connect(DB_PATH)
        for suffix, val, label in (("BUY", args.settle_buy, "买入"),
                                   ("SELL", args.settle_sell, "卖出")):
            if val is None:
                continue
            key = f"{fx.ccy_pair}_SETTLE_{suffix}"
            c.execute(
                "INSERT OR REPLACE INTO fx_rate(ccy_pair,rate_date,rate,source,fetched_at) "
                "VALUES(?,?,?,?,?)",
                (key, d, val, "user_manual_settle", now))
            print(f">>> 手动喂入 {key} = {val} @ {d}  ({label}结算汇兑比率)")
        c.commit()
        c.close()
        if args.settle_buy is not None:
            print(f"    (赎回实际净值 = nav_est × {args.settle_buy} / 中间价)")
            print("    · 主看板「实际折溢价(T-1结算)」= 行 T 取 rate_date<T 的最新一条（当天不公布）")
            print("    · 历史页「实际折溢价(T结算)」  = 行 D 取 rate_date=D 的当日真实值（盘后已公布）")
        return
    if args.fetch_chinamoney:
        res = FXProvider("513590").fetch_chinamoney()
        print(f">>> ChinaMoney 抓取: {res}")
        return
    if args.seed:
        n = FXProvider("513590").seed_rates()
        print(f">>> seed 历史 HKD/CNY 中间价: {n} 条 (9-10/9-11)")
        return

    # 默认全流程：建表 + seed历史 + 抓今日ChinaMoney
    n = FXProvider("513590").seed_rates()
    print(f">>> seed 历史 HKD/CNY 中间价: {n} 条 (9-10/9-11)")
    res = FXProvider("513590").fetch_chinamoney()
    print(f">>> ChinaMoney 抓今日中间价: {res}")

    fx = FXProvider("513590")
    print("\n=== FXProvider 各取数路径（严格模式，无兜底） ===")
    print(f"  ccy_pair               = {fx.ccy_pair}")
    print(f"  config 参考 fx(不进计算) = {fx.config_fx_ref}")
    try:
        print(f"  get_rate(9-11)         = {fx.get_rate('2026-09-11')}")
    except FXMissingError as e:
        print(f"  get_rate(9-11)         = [缺] {e}")
    try:
        print(f"  get_rate(最新)          = {fx.get_rate()}  (来自 fx_rate 表)")
    except FXMissingError as e:
        print(f"  get_rate(最新)          = [缺] {e}")
    sc = fx.sanity_check()
    if sc and sc.get("db_rate"):
        flag = "⚠️ 偏离过大" if abs(sc["dev_pct"]) > 1.0 else "✅ 正常"
        print(f"  sanity: ChinaMoney {sc['chinamoney']} ({sc['rate_date']}) "
              f"vs 表 {sc['db_rate']} ({sc['db_source']}) "
              f"({sc['dev_pct']:+.2f}%) {flag}")


if __name__ == "__main__":
    main()
