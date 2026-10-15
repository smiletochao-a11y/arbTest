#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/hk_quote.py — P2: 港股行情抓取（腾讯实时 + 日K / 复权日K）
==============================================================
为 IOPV 引擎提供成分股价格来源（513590 等港股通 ETF）。

  实时    : qt.gtimg.cn/q=hkXXXXX                              -> 现价            [3] 字段
  不复权日K: web.ifzq.gtimg.cn/appstock/app/kline/kline         -> 真实成交收盘价   [2] 字段
  前复权日K: web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?...,qfq -> 前复权价 + 除权除息元数据 [6]

⚠️⚠️ 价格口径铁律（2026-09-15 实测钉死，详见 docs/21）：
  交易所 PCF「替代金额」用的参考价 =
      **不复权收盘价(D)  −  Σ{ 每股股息(E) : D < 除权日 E ≤ T }**
  D = 篮子定价基准日（= PCF 的 PreTradingDay）；T = 该篮子所属公告的 TradingDay。

  含义：等价于「前复权价，但只调整到 T 日为止」——
  只扣掉「公告日 T 当天除息」的股息，不扣 T 之后才除息的股息。

  实测（9-10 / 9-11 / 9-14 三篮子全成分，误差 ≤ 0.000012%）：
    · 直接前复权(锚定今天)：9-10 篮子虚低 2.77bp（00551 裕元 9-14 才除息 0.40，被提前扣了）
    · 直接用不复权      ：9-10 篮子高 15.5bp（00288 万洲 9-11 除息 0.20，该扣没扣）
    · 本模块规则        ：三个篮子全部零误差
  ⇒ **禁止**用纯 qfq 或纯不复权算篮子；必须走 get_reference_closes()。

限流：日K逐只拉取，每 10 只 sleep 1s（东财 push2 已全段封禁，腾讯接口须克制）。
"""
import os
import re
import ssl
import json
import time
import sqlite3
import datetime
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import db as _db   # noqa: E402  core/db.py —— 单库方案 arb_master.db
DB_PATH = _db.DB_PATH   # 单库方案：统一 arb_master.db（与 arbTest 同文件）

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

RAW_KLINE_TPL = ("https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                 "?param=hk{code},day,{start},{end},{count}")
QFQ_KLINE_TPL = ("https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
                 "?param=hk{code},day,{start},{end},{count},qfq")

# 股息文本形如 "中期息0.4港元;" / "中期息0.087人民币;相当于0.101港元;" → 取港元金额
# 🔴 必须 findall **求和**，不能 search 取第一笔：同一天可能派多笔 ——
#    实测 06699 时代天使 2026-09-10 的 FHcontent = "中期息0.47港元;中期特别息4.1港元;"
#    只取第一笔 → 少扣 4.10 港元/股 → 159366 当日篮子虚高 **+4.41bp**（已复现定位）。
_DIV_RE = re.compile(r"([\d.]+)\s*港元")

# 🔴🔴 实时港股行情源（2026-09-17 实测钉死，docs/40 §七·8）
# ---------------------------------------------------------------------------
# **腾讯 `qt.gtimg.cn/q=hkXXXXX` 的港股行情是 15 分钟延迟行情。**
#   实证（同一时刻两次采样，间隔 45 秒）：
#       本机 14:45:54 → 行情时间戳 [30] = 2026/09/17 14:30:52
#       本机 14:46:39 → 行情时间戳 [30] = 2026/09/17 14:31:38
#    两笔均恒定滞后 **15 分 01~02 秒**；同请求里的 A 股 ETF（`sh513180`）时间戳
#    则等于本机时间（实时）。⇒ 延迟是港股口的**固有属性**，不是网络慢。
#
# 后果：用腾讯港股价算逐券 IOPV ⇒ 篮子价是「15 分钟前」的，却拿去和**实时**的
#   ETF 市价相减 ⇒ 折溢价被污染。实测 24 只亚洲逐券标的的绝对值中位误差
#   **5.4bp ⇒ 2.5bp**（换用下列实时源后腰斩），且系统性负偏置消失。
#
# 新浪 `hq.sinajs.cn/list=rt_hkXXXXX` 是**实时**口，字段（`,` 分隔）：
#     [2]今开 [3]昨收 [4]最高 [5]最低 [6]现价 [12]成交量 [17]日期 [18]时间 [19]状态
#   · `[19]` 前缀 `'30'` = 正常、**`'20'` = 停牌**
#     实证 200 只样本：197 只 `'30|3'`、3 只 `'20|7'`；后者恰为微创系
#     `00853/02172/02252` —— 与腾讯口 `[76]=='2'` 判出的**同 3 只**，两源口径一致。
#   · 停牌券同时满足：今开=最高=最低=`0.000`、现价=昨收、时间戳冻结在 `09:20`。
#   ⚠️ `[19]` 才是判据；**不要用「成交量为 0」**（开盘前正常券量亦为 0，会误报）。
#   价格与腾讯口**同口径**（真实成交价，无复权问题），可直接互换。
#
# 停牌券的「现价」= 停牌前收盘价（`[6]` == `[3]`），正是交易所 IOPV 口径要求的
#   估值价 ⇒ **照算不拦**，只把「停牌」这件事暴露给上层标注（与既有红线一致）。
_SINA_RT_URL = "https://hq.sinajs.cn/list="
_SINA_REF = "https://finance.sina.com.cn/"
_SINA_STATUS_OK = "30"
_SINA_STATUS_SUSPENDED = "20"
_SINA_CHUNK = 60


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class HKQuoteFetcher:
    def __init__(self):
        # 🔴 内存日K缓存（prefetch 灌入）：{code: {date: 收盘}} / {code: [(除息日, 股息, 原文)]}
        #    用途：历史回填/批量校准时，逐日逐只请求会放大到上万次（340 篮子 × 48 只），
        #    预热一次后变成「每只一次」。缓存一旦存在即视为覆盖任意子区间（见 prefetch 注释）。
        self._raw_cache = {}
        self._exdiv_cache = {}

    # ---- 基础请求 ----
    def _get(self, url, ref="https://finance.qq.com/", encoding="gbk"):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": ref})
        return urllib.request.urlopen(req, timeout=20, context=CTX).read().decode(encoding, "ignore")

    def _get_json(self, url, ref="https://gu.qq.com/"):
        return json.loads(self._get(url, ref=ref, encoding="utf-8"))

    @staticmethod
    def _kline_url(code, start, end, count=10, qfq=False):
        tpl = QFQ_KLINE_TPL if qfq else RAW_KLINE_TPL
        return tpl.format(code=code, start=start, end=end, count=count)

    # ---- 实时 ----
    @staticmethod
    def _hk_code(c):
        """港股行情口代码规范化：腾讯要 **5 位补零**（`hk01093` ✅ / `hk1093` ❌ 返回空）。

        🔴 2026-09-17（docs/40 §七·5）：沪市 PCF 给的是**不定长**码（`1093` / `13`），
        深市给的是已补零码（`00241`）⇒ 必须在行情层统一 `zfill(5)`。
        非纯数字（异常输入）原样返回，不硬凑。"""
        s = str(c or "").strip()
        return s.zfill(5) if s.isdigit() else s

    # ---- 新浪实时口（默认源，见文件头红线） ----
    def _sina_rt_map(self, norm):
        """`norm` = {原始码: 5 位补零码} -> {原始码: (现价, 停牌, 昨收, 时间, 成交量, 今开)}。

        按 `_SINA_CHUNK` 分批；**取不到的码不进结果**（调用方按缺失处理，不兜底）。
        停牌券不剔除：其现价即停牌前收盘价，正是 IOPV 要用的估值价。"""
        out = {}
        # 🔴 反向映射必须**多对一**：沪市 PCF 给不定长码（`700` / `13` / `1093`），
        #    深市给已补零码（`00700`），两者补零后**同码**。若用
        #    `{补零码: 原始码}` 的一对一字典，只有一个原始码能拿到价，
        #    另一个会被判「实时价缺失」⇒ 跨基金取并集时**必然踩**（2026-09-17 实测：
        #    20 只亚洲标的并集 318 码，9 只标的因此报缺失 10~34 只）。
        rev = {}
        for raw, pad in norm.items():
            rev.setdefault(pad, []).append(raw)
        items = sorted(rev)
        for i in range(0, len(items), _SINA_CHUNK):
            batch = items[i:i + _SINA_CHUNK]
            url = _SINA_RT_URL + ",".join("rt_hk" + c for c in batch)
            try:
                raw = self._get(url, ref=_SINA_REF)
            except Exception:
                continue
            for line in raw.strip().split(";"):
                m = re.search(r'hq_str_rt_hk(\d+)="(.*)"', line)
                if not m:
                    continue
                f = m.group(2).split(",")
                if len(f) < 20:
                    continue
                try:
                    price = float(f[6])
                    prev = float(f[3])
                except ValueError:
                    continue
                if price <= 0:
                    continue                                  # 无效码 / 无昨收 ⇒ 缺失
                vol = _num(f[12], 0.0)                        # [12] 成交量（股）
                op = _num(f[2], 0.0)                          # [2] 今开
                hit = (price, f[19].startswith(_SINA_STATUS_SUSPENDED), prev, f[18], vol, op)
                for raw_code in rev.get(m.group(1), (m.group(1),)):
                    out[raw_code] = hit
        return out

    # ---- 实时 ----
    def get_realtime(self, codes, source="sina_rt"):
        """codes: list of HK codes e.g. ['00142','00700'] -> {code: 现价}

        `source`（2026-09-17 立）：
          `'sina_rt'`（**默认**）新浪 `rt_hk` —— **实时**，与官方 IOPV 同节奏；
          `'tencent'`  腾讯 `hk` —— **15 分钟延迟**，保留仅为对照/回退。
        返回键 = **调用方传入的原始 code**（内部按 5 位补零后再请求），调用方无感。"""
        if not codes:
            return {}
        norm = {c: self._hk_code(c) for c in codes}
        if source == "tencent":
            return {c: v[0] for c, v in self._tencent_marks(norm).items()}
        return {c: v[0] for c, v in self._sina_rt_map(norm).items()}

    # ---- 腾讯口（15 分钟延迟，仅对照/回退用） ----
    def _tencent_marks(self, norm):
        rev = {}
        for raw, pad in norm.items():
            rev.setdefault(pad, []).append(raw)          # 同样多对一，见 _sina_rt_map
        q = ",".join("hk" + v for v in sorted(rev))
        raw = self._get("https://qt.gtimg.cn/q=" + q)
        out = {}
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r'v_hk(\d+)="(.*)"', line)
            if not m:
                continue
            f = m.group(2).split("~")
            try:
                price = float(f[3])                   # [3] = 当前价
            except (IndexError, ValueError):
                continue
            hit = (price, len(f) > 76 and f[76] == self._SUSPENDED_STATUS,
                   _num(f[6], 0.0) if len(f) > 6 else 0.0,      # [6] 成交量
                   _num(f[5], 0.0) if len(f) > 5 else 0.0)      # [5] 今开
            for raw_code in rev.get(m.group(1), (m.group(1),)):
                out[raw_code] = hit
        return out

    # ---- 实时 + 交易状态 ----
    # 🔴 停牌判定字段（2026-09-17 实测钉死）：
    #    新浪 `rt_hk`：`[19]` 前缀 `'20'` = 停牌（`'30'` = 正常）—— **现用判据**；
    #    腾讯 `hk`   ：`[76]` `'2'` = 停牌（`'1'` = 正常）—— 仅 `source='tencent'` 时用。
    #    两侧**同为**停牌时同时满足：成交量 / 成交额恒为 0、今开 = 0.000、
    #    现价 = 停牌前收盘价（即交易所 IOPV 口径要用的估值价）。
    #    实测：159366 的 48 只成分中**恰好 3 只**（00853 / 02172 / 02252，微创系
    #    自 2026-09-01 停牌）被两源一致判为停牌，其余 45 只均正常。
    #    ⚠️ **不要用「成交量为 0」判停牌** —— 开盘前（如 09:20）正常券成交量也是 0，会误报。
    _SUSPENDED_STATUS = "2"

    def get_realtime_marks(self, codes, source="sina_rt", detail=False):
        """实时价 + 停牌标记 -> {code: (现价 float, 是否停牌 bool)}。

        **不用于改价**：停牌券的现价本身就是停牌前收盘价，正是交易所 IOPV 口径
        要求的估值价；本方法只是把「这只券停牌」这件事暴露出来，供上层标注
        「停牌中 · 暂停参考」。价格不可解析的代码**不进结果**（不兜底）。
        返回键 = **调用方传入的原始 code**（内部按 5 位补零请求）。
        `source` 见 `get_realtime()`（默认实时口）。

        `detail=True`（2026-09-17 加，为单券 FoF 的「报价冻结」判据）时返回
        **4 元组** `(现价, 停牌, 成交量, 今开)`：`成交量 == 0 且 今开 == 0`
        ⇒ 该券**今日零成交、报价冻结在昨收**（实证 `2830` 南方沙特 / `3167`
        工银南方中国），其市价不可当篮子价用（见 `etf_iopv_quote`）。
        ⚠️ 只能在**盘中**用「零成交」判冻结；开盘前所有券皆零成交，会全体误判。"""
        if not codes:
            return {}
        norm = {c: self._hk_code(c) for c in codes}
        if source == "tencent":
            m = self._tencent_marks(norm)
        else:
            m = self._sina_rt_map(norm)
        if detail:
            return {c: (v[0], v[1], v[4], v[5]) for c, v in m.items()}
        return {c: (v[0], v[1]) for c, v in m.items()}

    # ---- 不复权日K（真实成交收盘） ----
    def get_raw_closes(self, codes, start, end):
        """逐只拉【不复权】日K -> {code: {date: 收盘}}。date 形如 '2026-09-10'。

        命中 prefetch 缓存则直接按 [start,end] 切片（零网络请求）。

        🔴 请求前统一走 `_hk_code()` 补零（2026-09-17 修，docs/40 §七·5 同源问题）：
          沪市 `sse_xml` 给的是**不定长**码（`1093` / `13`），直接拼 `hk1093` 腾讯返回空
           ⇒ 旧行为是**静默 `{}`**（调用方误判成「该券没有日K」）。返回键仍是**调用方传入的原始 code**。"""
        out = {}
        for i, c in enumerate(codes):
            cached = self._raw_cache.get(c)
            if cached is not None:
                out[c] = {d: v for d, v in cached.items() if start <= d <= end}
                continue
            q = self._hk_code(c)
            try:
                d = self._get_json(self._kline_url(q, start, end))
                kl = (d.get("data") or {}).get("hk" + q, {}).get("day") or []
                out[c] = {x[0][:10]: float(x[2]) for x in kl}   # [2] = 收盘
            except Exception:
                out[c] = {}
            if (i + 1) % 10 == 0:
                time.sleep(1.0)   # 限流保护
        return out

    # ---- 除权除息事件（借前复权日K的 cqr/FHcontent 元数据） ----
    def get_ex_dividends(self, codes, start, end):
        """逐只拉【前复权】日K，提取「除息事件」。

        ⚠️ 字段语义（2026-09-15 实测钉死）：
          · `cqr` 不是除权日 —— 它出现在几乎每根 K 线上（实为「当日公告日」，
            回购公告也会带上），**用它会全是误报**。
          · 真正的除息标记是 **`FHcontent`（分红内容）非空**，该 K 线日期即除净日。
            若某根 K 线的 FHcontent 解析不出港元股息（送股/以股代息等非现金情形），
            股息置 None，由上层标 WARN，**绝不猜**。

        返回 {code: [(ex_date 'YYYY-MM-DD', 每股股息港元 float|None, 原文 str), ...]}

        注：腾讯该接口不严格遵守 start/end，常返回最近约 10 根 K 线；
        多出来的日期由调用方按 (D, T] 区间过滤，不影响正确性。
        命中 prefetch 缓存则直接返回（零网络请求）。"""
        out = {}
        for i, c in enumerate(codes):
            cached = self._exdiv_cache.get(c)
            if cached is not None:
                out[c] = cached
                continue
            evs = []
            q = self._hk_code(c)          # 补零请求（见 get_raw_closes 注释）
            try:
                d = self._get_json(self._kline_url(q, start, end, qfq=True))
                kl = (d.get("data") or {}).get("hk" + q, {}).get("qfqday") or []
                for x in kl:
                    meta = x[6] if len(x) > 6 and isinstance(x[6], dict) else {}
                    content = (meta.get("FHcontent") or "").strip()
                    if not content:
                        continue
                    _divs = [float(v) for v in _DIV_RE.findall(content)]
                    evs.append((x[0][:10], round(sum(_divs), 6) if _divs else None, content))
            except Exception:
                pass
            out[c] = evs
            if (i + 1) % 10 == 0:
                time.sleep(1.0)
        return out

    # ---- 核心：按交易所口径还原参考价 ----
    def get_reference_closes(self, codes, basket_date, announce_date):
        """按交易所 PCF 口径返回篮子参考价。

        ref(D) = 不复权收盘(D) − Σ{ 股息(E) : D < E <= T }

        参数：basket_date / announce_date 均为 'YYYY-MM-DD'（D = 基准日，T = 公告日）。
        返回 (ref, raw, drops, unparsed)：
          ref      {code: 参考价}      —— 算篮子用这个
          raw      {code: 不复权收盘}  —— 供对照
          drops    {code: 扣减股息额}   —— 供展示/排错
          unparsed [code]              —— 该股在 (D,T] 内除权但股息解析不出（非现金除权），上层须 WARN
        """
        raw = self.get_raw_closes(codes, basket_date, basket_date)
        evs = self.get_ex_dividends(codes, basket_date, announce_date)
        ref, drops, unparsed = {}, {}, []
        for c in codes:
            rc = raw.get(c, {}).get(basket_date)
            if rc is None:
                continue
            total = 0.0
            bad = False
            for (e, div, _txt) in evs.get(c, []):
                if basket_date < e <= announce_date:
                    if div is None:
                        bad = True
                    else:
                        total += div
            if bad:
                unparsed.append(c)
            ref[c] = rc - total
            if total:
                drops[c] = round(total, 4)
        return ref, raw, drops, unparsed

    # ---- 批量预热（历史回填专用） ----
    def prefetch(self, codes, start, end):
        """一次性把 [start,end] 的日K + 除息事件灌入内存缓存。

        为什么要它：`IOPVVerifier.verify_all_pending()` 会对**每个篮子基准日**各拉一遍
        全部成分的日K（340 日 × 48 只 ≈ 1.6 万次请求 + 同量级的前复权请求）。
        预热后每只股票只发 **2 次**请求（不复权 + 前复权），整段历史 ≈ 100 次。

        ⚠️ 缓存一旦写入即被当作"覆盖任意子区间"（`get_raw_closes` 只做日期切片）——
        故本方法只用于**历史回填**这种"范围一次定死"的场景；
        日常逐日校准不要调它，否则会拿到过期缓存。
        返回预热的股票数。"""
        codes = [c for c in codes if c not in self._raw_cache]
        n = 0
        for i, c in enumerate(codes):
            q = self._hk_code(c)          # 补零请求（见 get_raw_closes 注释）
            try:
                d = self._get_json(self._kline_url(q, start, end, count=500))
                kl = (d.get("data") or {}).get("hk" + q, {}).get("day") or []
                self._raw_cache[c] = {x[0][:10]: float(x[2]) for x in kl}
                n += 1
            except Exception:
                self._raw_cache[c] = {}
            try:
                d = self._get_json(self._kline_url(q, start, end, count=500, qfq=True))
                kl = (d.get("data") or {}).get("hk" + q, {}).get("qfqday") or []
                evs = []
                for x in kl:
                    meta = x[6] if len(x) > 6 and isinstance(x[6], dict) else {}
                    content = (meta.get("FHcontent") or "").strip()
                    if not content:
                        continue
                    _divs = [float(v) for v in _DIV_RE.findall(content)]
                    evs.append((x[0][:10], round(sum(_divs), 6) if _divs else None, content))
                self._exdiv_cache[c] = evs
            except Exception:
                self._exdiv_cache[c] = []
            if (i + 1) % 10 == 0:
                time.sleep(1.0)
        return n

    # ---- 停牌券：按「前收盘」估值 ----
    def get_last_close_asof(self, codes, iso_date):
        """取每只股票 **≤ iso_date 的最后一根**不复权日K → {code: (成交日, 收盘)}。

        ⚠️ 与"兜底造数"的区别：这不是猜一个价 —— 交易所对**停牌证券**的估值惯例就是
        取最近成交价（前收盘）。但停牌股一旦复牌跳空，按前收盘算出的篮子会与实际
        补券成本背离 ⇒ **调用方必须把"用了前收盘"这件事标出来**
        （status=WARN + 列明 `code@成交日`），不得静默。"""
        if not codes:
            return {}
        d0 = (datetime.date.fromisoformat(iso_date) - datetime.timedelta(days=400)).isoformat()
        raw = self.get_raw_closes(codes, d0, iso_date)
        out = {}
        for c in codes:
            bars = {d: v for d, v in (raw.get(c) or {}).items() if d <= iso_date}
            if bars:
                last = max(bars)
                out[c] = (last, bars[last])
        return out

    def get_reference_closes_asof(self, codes, basket_date, announce_date):
        """停牌券专用：用 **≤ basket_date 的最后一根**不复权收盘，套用**同一条**参考价规则
            ref(D) = 不复权收盘(D) − Σ{ 股息(E) : D < E ≤ T }
        返回 (ref, suspended)：ref {code: 参考价}；suspended {code: 实际取价日}。"""
        asof = self.get_last_close_asof(codes, basket_date)
        if not asof:
            return {}, {}
        evs = self.get_ex_dividends(list(asof.keys()), basket_date, announce_date)
        ref, suspended = {}, {}
        for c, (pdate, close) in asof.items():
            total = 0.0
            for (e, div, _txt) in evs.get(c, []):
                if basket_date < e <= announce_date and div is not None:
                    total += div
            ref[c] = close - total
            suspended[c] = pdate
        return ref, suspended

    # ---- 兼容旧调用（历史校准） ----
    def get_history(self, codes, start, end, step=15, adjusted=False):
        """adjusted=False（默认）= 不复权；True = 前复权。返回 {code: {date: close}}。"""
        if not adjusted:
            return self.get_raw_closes(codes, start, end)
        out = {}
        for i, c in enumerate(codes):
            q = self._hk_code(c)          # 补零请求（见 get_raw_closes 注释）
            try:
                d = self._get_json(self._kline_url(q, start, end, qfq=True))
                node = (d.get("data") or {}).get("hk" + q, {})
                kl = node.get("qfqday") or node.get("day") or []
                out[c] = {x[0][:10]: float(x[2]) for x in kl}
            except Exception:
                out[c] = {}
            if (i + 1) % 10 == 0:
                time.sleep(1.0)
        return out

    def get_closes_for_trade_date(self, fund_code, trade_date, db=DB_PATH):
        """读 PCF 成分股 + 该日收盘，返回 (closes, shares_map)。供历史校准。"""
        c = sqlite3.connect(db)
        rows = c.execute(
            "SELECT stock_code, shares FROM pcf_component WHERE fund_code=? AND trade_date=?",
            (fund_code, trade_date)).fetchall()
        c.close()
        codes = [r[0] for r in rows]
        shares_map = {r[0]: r[1] for r in rows}
        iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        hist = self.get_raw_closes(codes, iso, iso)
        closes = {code: hist[code].get(iso) for code in codes if iso in hist.get(code, {})}
        return closes, shares_map


# ----------------------------------------------------------------------
# 模块自检（非正式 IOPV 计算，仅验证数据链路 + 参考价口径）
# ----------------------------------------------------------------------
def main():
    import importlib.util
    ff = HKQuoteFetcher()

    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT stock_code, shares FROM pcf_component WHERE fund_code='513590' "
        "AND trade_date=(SELECT MAX(trade_date) FROM pcf_component WHERE fund_code='513590')"
    ).fetchall()
    c.close()
    codes = [r[0] for r in rows]

    print(f">>> [实时] 拉取 {len(codes)} 只成分港股现价")
    rt = ff.get_realtime(codes)
    print(f"    取到 {len(rt)}/{len(codes)} 只")

    print("\n>>> [参考价口径] 用 00551 裕元（9-14 除息 0.40）演示三种口径差异")
    ref, raw, drops, unp = ff.get_reference_closes(["00551", "00288", "00142"], "2026-09-10", "2026-09-11")
    print(f"    D=2026-09-10, T=2026-09-11")
    for k in ref:
        print(f"      hk{k}: 不复权={raw.get(k, {}).get('2026-09-10')}  参考价={ref.get(k)}  "
              f"扣股息={drops.get(k, 0)}")
    print("    预期：00288 扣 0.20（T 日除息）；00551 不扣（9-14 才除息，晚于 T）")
    print("\n提示：正式计算/校准/落库 iopv_verify 见 core/iopv_calc.py。")


if __name__ == "__main__":
    main()
