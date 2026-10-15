# -*- coding: utf-8 -*-
"""
回填指数历史收盘到 ETFarb 独立库 etfarb.db（不再写 arb_master.db，物理隔离 LOF 实盘）。

为什么需要：
    实时接口只能拿「最新值」；历史回测/验证估值算法需要数月日线。
    东财 kline 接口（push2his）在用户本机也可能被反爬（IP 级封禁），
    表现为 "Remote end closed connection without response" —— 这是东财红线禁区。

三种用法（按推荐顺序）：

1) 浏览器代抓 + 本地灌库（最稳，零程序化轰炸东财）：
   东财官网 F12 → Network 里找到 kline 请求 → 右键 Response 存成 .json
   -> python core/backfill_gpcsp_history.py FISAULMU --file C:\\path\\fisaulmu.json
   也兼容 push2 实时 JSONP（带 f43 的那种），会存成「今日」单点。

2) 直连 push2his（仅在你 IP 未被封时有效，单次按需，勿高频）：
   -> python core/backfill_gpcsp_history.py [GPCSP006|FISAULMU|ALL] [回补天数, 默认 120]

3) 直接给浏览器 F12 的真实链接（同样受 push2his 封禁影响）：
   -> python core/backfill_gpcsp_history.py FISAULMU --url "https://push2his...secid=100.FISAULMU..."

说明：
    - 纯 stdlib，不依赖 arbTest 代码。
    - kline 的 close(f53) 为真实点位（与 push2 实时 f43×100 不同），原样落库；
      push2 实时 f43 已是 ×100，入库前 /100 还原成真实点位，保持口径一致。
    - 运行后把 (symbol,date) 真实收盘写入 etfarb.db.index_history，source='eastmoney'，覆盖旧行。
"""
import sys
import json
import time
import urllib.request
from datetime import date as _date

# ETFarb 独立数据层（etfarb.db）；脚本在 core/ 目录运行，db.py 同目录可直接 import
import db

SYMBOLS = {
    "GPCSP006": "249.GPCSP006",   # 富时亚太精选指数（159687 基准）
    "FISAULMU": "100.FISAULMU",   # 富时沙特阿拉伯指数（159329 基准，secid 前缀 100 已浏览器验证）
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def http_get_json(url, tries=3):
    last = None
    for attempt in range(tries):
        for use_proxy in (False, True):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({} if not use_proxy else {})
            )
            try:
                with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=15) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except Exception as e:
                last = e
                continue
        if attempt < tries - 1:
            time.sleep(2 + attempt * 2)
    print("  [WARN] 请求失败: %s" % last)
    return None


def fetch_kline(secid, limit=120):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, limit))
    d = http_get_json(url)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    out = []
    for kl in d["data"]["klines"]:
        p = kl.split(",")
        out.append((p[0], float(p[2])))  # f51=date, f53=close（真实点位）
    return out


def strip_jsonp(s):
    """东财常把 JSON 包成 jQueryxxx({...}); 剥掉首尾非 JSON 部分。"""
    s = s.strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return s
    start = min(starts)
    ends = [i for i in (s.rfind("}"), s.rfind("]")) if i >= 0]
    end = max(ends)
    return s[start:end + 1]


def parse_kline_or_realtime(d):
    """从东财响应解析出 (date, close) 序列。
    支持两种结构：
      - push2his kline：{"data":{"klines":["date,o,c,h,l,v", ...]}}
      - push2 实时 JSONP：{"data":{"f43": 154323, ...}}  -> 存成「今日」单点
    """
    if not isinstance(d, dict):
        return None
    data = d.get("data")
    # kline 序列
    klines = None
    if isinstance(data, dict):
        klines = data.get("klines")
        if not klines and isinstance(data.get("data"), dict):
            klines = data["data"].get("klines")
    if klines:
        out = []
        for kl in klines:
            p = kl.split(",")
            if len(p) < 3:
                continue
            out.append((p[0], float(p[2])))
        return out or None
    # push2 实时单点（f43 为 ×100，还原真实点位）
    if isinstance(data, dict) and "f43" in data:
        try:
            val = float(data["f43"]) / 100.0
        except (TypeError, ValueError):
            return None
        today = _date.today().strftime("%Y-%m-%d")
        return [(today, val)]
    return None


def parse_file(path):
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception as e:
        print("  [ERROR] 读文件失败: %s" % e)
        return None
    raw = strip_jsonp(raw)
    try:
        d = json.loads(raw)
    except Exception as e:
        print("  [ERROR] JSON 解析失败: %s" % e)
        return None
    return parse_kline_or_realtime(d)


def save(symbol, rows):
    conn = db.get_conn()
    n = 0
    for date, close in rows:
        conn.execute(
            "INSERT OR REPLACE INTO index_history (symbol, date, close, source) VALUES (?, ?, ?, ?)",
            (symbol, date, close, "eastmoney"),
        )
        n += 1
    conn.commit()
    conn.close()
    return n


def _ingest(symbol, rows, label):
    print("  [解析] %s %d 行，首/尾: %s ... %s" % (label, len(rows), rows[0], rows[-1]))
    n = save(symbol, rows)
    print("  [写入] etfarb.db.index_history(%s) 共 %d 行, source=eastmoney" % (symbol, n))


def main():
    args = sys.argv[1:]
    file_path = None
    url = None
    symbol = "ALL"
    limit = 120
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--file", "-f"):
            file_path = args[i + 1]; i += 2; continue
        if a in ("--url", "-u"):
            url = args[i + 1]; i += 2; continue
        if a in ("--limit", "-l"):
            limit = int(args[i + 1]); i += 2; continue
        if a.startswith("--file="):
            file_path = a.split("=", 1)[1]; i += 1; continue
        if a.startswith("--url="):
            url = a.split("=", 1)[1]; i += 1; continue
        if a.lstrip("-").isdigit():
            limit = int(a.lstrip("-")); i += 1; continue
        symbol = a.upper(); i += 1

    if file_path:
        if symbol == "ALL":
            symbol = "FISAULMU"
            print("  [提示] --file 模式未指定 symbol，默认 FISAULMU")
        print("[文件灌库] %s <- %s" % (symbol, file_path))
        rows = parse_file(file_path)
        if not rows:
            print("  [失败] 未能从文件解析出 K线/实时数据（确认是东财 kline 或 push2 的 JSON 响应）")
            return
        _ingest(symbol, rows, "文件")
        return

    if url:
        if symbol == "ALL":
            symbol = "FISAULMU"
        print("[URL抓取] %s <- %s" % (symbol, url))
        d = http_get_json(url)
        rows = parse_kline_or_realtime(d) if d else None
        if not rows:
            print("  [失败] 该 URL 未取到数据。若 push2his 被封，本机直连也会失败；请用 --file 浏览器存盘。")
            return
        _ingest(symbol, rows, "URL")
        return

    # 直连 push2his（自动模式）
    targets = SYMBOLS if symbol == "ALL" else {symbol: SYMBOLS[symbol]}
    for sym, secid in targets.items():
        print("[回填] %s (%s) 历史收盘, 回补 %d 日..." % (sym, secid, limit))
        rows = fetch_kline(secid, limit)
        if not rows:
            print("  [失败] kline 未取到数据。多半是本机 IP 被东财反爬拦截（Remote end closed）。")
            print("          改用浏览器 F12 存 kline JSON -> --file 灌库（见文件头说明）。")
            continue
        _ingest(sym, rows, "自动")


if __name__ == "__main__":
    main()
