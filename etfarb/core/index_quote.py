# -*- coding: utf-8 -*-
"""LOF / 现金管理 估值用的「指数涨跌幅」行情通道（P1 迁移，2026-09-18，docs/50）。

🔴 职责边界：本模块**只为 LOF 估值提供「跟踪指数涨跌幅」**，与跨境 ETF 的 IOPV 模型
（IOPVCalculator）无关。LOF 估值公式 = `NAV × (1 + 指数涨跌幅%)`，与集思录「灰底估值」
口径一致（东哥 09-18 拍板：集思录对无 IOPV 的基金正是用这套）。

源映射（照搬 arbTest/ArbDashboard fund_service.py 的 proven 通道，2026-09-18 核对）
---------------------------------------------------------------------------
· A 股指数（000xxx / 399xxx 等 6 位）：新浪 `s_sh`/`s_sz` 优先，腾讯 `qt.gtimg` 兜底
  （中证系 CSI 码经 `_INDEX_CODE_MAP` 映射到 A 股 6 位码后走同一通道）。
· 港股指数（HSI / HSCEI / HSTECH / HSMI / HSSI / HSCI / HSSCNE 等）：新浪 `rt_hk`。
· 个别未映射的 CSI 码（930914 / 930917）：东财 `push2delay`（2.secid）。
· 无源（950090 等经映射落到有源码；真正无解的）→ 返回 None，前端显「—」。

🔴 指数历史（K 线）不在这里处理：P1 看板只需**实时涨跌幅**；历史用 etfarb.db
`index_history` 的每日收盘记录器积累（woody/HSSI 同款），不在本模块。
"""
import json
import ssl
import time
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SINA_REF = {"Referer": "https://finance.sina.com.cn/",
             "Accept": "text/event-stream"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 非代码符号 → A 股 6 位码（照搬 arbTest _INDEX_CODE_MAP）
_INDEX_CODE_MAP = {
    '中小100': '399011', '移动互联': '399363', '中证500': '000905',
    '中证TMT': '000998', '中证白酒': '399997', '中证消费': '399932',
    '中证医疗': '399812', '中证银行': '399986', '国证有色': '399395',
    '证券公司': '399975', '国企改': '399974',
    'SZ399989': '399989', 'SZ399990': '399990', 'SZ399993': '399993',
    'H30094': '2.H30094', '950090': '2.950090',
    '930713': '2.930713', '930875': '2.930875',
    '930720': '2.930720', '930997': '2.930997',
    '000922': '000922', '000961': '000961', '000979': '000979',
    'CES300.HI': None,   # 中华沪深港300：东财无标准源，显「—」
    'KWEB': None, 'RSPH': None,
}

# 港股指数前缀（新浪 rt_hk 直连）
_HK_PREFIXES = (
    'HSI', 'HSTECH', 'HSCEI', 'HSCI', 'HSCCI', 'HSSCNE', 'HSSI', 'HSMI',
    'HSHK', 'HSCIC', 'HSI50', 'HSML25', 'HSCON', 'HSFIN', 'HSIND',
    'HSENER', 'HSUTIL', 'HSPROP', 'HSINFO', 'HSIT', 'HSMT', 'HSCONS',
    'HSMED', 'HSHEAL', 'HSRE', 'HSCOM', 'HSFIN25', 'HSHK50', 'HSINT',
    'HSREIT', 'HSUTIL',
)

# 东财 push2delay secid 前缀（中证系列）
_CSI_SECID_PREFIX = "2."

# 东财指数 secid（港股通/恒生/日经/中证自定义）：`100.HSI` `124.HSIII` `2.931637` `100.N225` `2.H30094`
def _is_em_secid(cs):
    if "." not in cs:
        return False
    mkt, rest = cs.split(".", 1)
    if not mkt.isdigit() or not rest:
        return False
    # rest 可能含 H(HK)/N(日经) 字母，其余须为数字/字母
    return rest.replace("H", "").replace("N", "").isalnum()


def clean_index_symbol(sym):
    """清洗 + 映射，返回可用于行情查询的 symbol（A 股 6 位码 / 港股名 / CSI 码 / ''）。"""
    s = (sym or "").strip().upper()
    if not s or s == "-":
        return ""
    if s in _INDEX_CODE_MAP:
        m = _INDEX_CODE_MAP[s]
        return m if m else ""
    if s.startswith("^"):
        s = s[1:]
    if s.endswith(".CSI"):
        s = s[:-4]
    if s.startswith("SZ") or s.startswith("SH"):
        s = s[2:]
    # 东财 A 股指数 secid（0.=深/1.=沪）→ 裸 6 位码：实时走 A 股实时通道
    #（sina/腾讯，非 push2delay 延迟口），历史走 fqkline 同一裸码。🔴 2./100./124.
    #   等前缀保留 secid 形态（930xxx 自定义指数只有东财有实时源）。
    if "." in s:
        mkt, rest = s.split(".", 1)
        if mkt in ("0", "1") and rest.isdigit():
            s = rest
    if s in _INDEX_CODE_MAP:
        m = _INDEX_CODE_MAP[s]
        return m if m else ""
    return s


def classify_index_symbol(sym):
    """返回 ('hk' | 'ashare' | 'csi' | 'nosource' | 'skip', clean_sym)。"""
    cs = clean_index_symbol(sym)
    if not cs:
        return "skip", cs
    if any(cs.startswith(p) for p in _HK_PREFIXES):
        return "hk", cs
    if cs.isdigit() and len(cs) == 6:
        return "ashare", cs
    if cs.startswith("93") and len(cs) == 6:
        return "csi", cs
    if cs.startswith("95"):
        return "nosource", cs
    if _is_em_secid(cs):
        return "em_secid", cs
    return "other", cs


def _get(url, ref=None, timeout=10):
    hdr = {"User-Agent": _UA}
    if ref:
        hdr.update(ref)
    req = urllib.request.Request(url, headers=hdr)
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode("gbk", "ignore")


class IndexQuoteProvider:
    """一轮看板一个实例，进程内按 clean_sym 缓存（同一只指数跨 LOF 只取一次）。

    方法 `get_change(idx_code)` → `{'pct': float, 'price': float}` 或 `None`（无源/失败）。
    """

    def __init__(self, cache_ttl=60):
        self._cache = {}
        self._cache_t = {}
        self._ttl = cache_ttl

    def get_change(self, idx_code):
        cs = clean_index_symbol(idx_code)
        kind, _ = classify_index_symbol(idx_code)
        if kind in ("skip", "nosource"):
            return None
        now = time.time()
        if cs in self._cache and (now - self._cache_t.get(cs, 0)) < self._ttl:
            return self._cache[cs]
        res = self._fetch(kind, cs)
        if res is not None:
            self._cache[cs] = res
            self._cache_t[cs] = now
        return res

    def _fetch(self, kind, cs):
        if kind == "ashare":
            return self._fetch_ashare(cs)
        if kind == "hk":
            return self._fetch_hk(cs)
        if kind == "csi":
            return self._fetch_csi(cs)
        if kind == "em_secid":
            return self._fetch_em_secid(cs)
        return None

    # ---- 东财指数 secid（港股通/恒生/日经/中证自定义）：push2delay ----
    def _fetch_em_secid(self, secid):
        try:
            url = ("https://push2delay.eastmoney.com/api/qt/stock/get?secid=%s"
                   "&fields=f43,f57,f58,f170&invt=2&fltt=2&_=%d"
                   % (secid, int(time.time() * 1000)))
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"})
            raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8")
            obj = json.loads(raw)
            d = obj.get("data") or {}
            pct = d.get("f170")
            price = d.get("f43")
            if pct is None or price is None:
                return None
            return {"pct": float(pct), "price": float(price)}
        except Exception:
            return None

    # ---- A 股指数：新浪 s_sh/sz + 腾讯兜底 ----
    def _fetch_ashare(self, code):
        prefix = "s_sz" if (code.startswith("399") or code.startswith("159")
                            or code.startswith("3999")) else "s_sh"
        try:
            raw = _get("http://hq.sinajs.cn/list=%s%s" % (prefix, code), ref=_SINA_REF)
            for line in raw.strip().splitlines():
                if '="' not in line:
                    continue
                parts = line.split('"')[1].split(",")
                if len(parts) >= 4 and parts[1]:
                    pct = float(parts[3])
                    price = float(parts[1])
                    if pct != 0.0 or price != 0.0:
                        return {"pct": pct, "price": price}
        except Exception:
            pass
        # 腾讯兜底
        try:
            tc = "sz" if code.startswith(("399", "159")) else "sh"
            raw = _get("http://qt.gtimg.cn/q=%s%s" % (tc, code),
                       ref={"Referer": "https://finance.qq.com/"})
            for line in raw.strip().split(";"):
                line = line.strip()
                if not line or "v_" not in line:
                    continue
                parts = line.split('"')[1].split("~")
                if len(parts) >= 33 and parts[32]:
                    return {"pct": float(parts[32]), "price": float(parts[3] or 0)}
        except Exception:
            pass
        return None

    # ---- 港股指数：新浪 rt_hk ----
    def _fetch_hk(self, code):
        try:
            raw = _get("http://hq.sinajs.cn/list=rt_hk%s" % code, ref=_SINA_REF)
            for line in raw.strip().splitlines():
                if '="' not in line:
                    continue
                parts = line.split('"')[1].split(",")
                if len(parts) >= 9 and parts[6]:
                    return {"pct": float(parts[8]), "price": float(parts[6])}
        except Exception:
            pass
        return None

    # ---- 个别未映射 CSI 码：东财 push2delay ----
    def _fetch_csi(self, code):
        try:
            secid = _CSI_SECID_PREFIX + code
            url = ("https://push2delay.eastmoney.com/api/qt/stock/get?secid=%s"
                   "&fields=f43,f57,f58,f170&_=%d" % (secid, int(time.time() * 1000)))
            raw = _get(url)
            import json
            obj = json.loads(raw)
            d = obj.get("data") or {}
            pct = d.get("f170")
            price = d.get("f43")
            if pct is None or price is None:
                return None
            return {"pct": float(pct), "price": float(price)}
        except Exception:
            return None


if __name__ == "__main__":
    import sys
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    p = IndexQuoteProvider()
    tests = ["HSI", "HSCEI", "HSMI", "399300", "000905", "930914.CSI", "950090.CSI",
             "中小100", "证券公司", "CES300.HI", "H30094.CSI", "移动互联", "-"]
    for t in tests:
        print("%-14s -> %s" % (t, p.get_change(t)))
