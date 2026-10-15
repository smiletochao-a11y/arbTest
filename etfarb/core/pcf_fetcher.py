# -*- coding: utf-8 -*-
"""
深交所 ETF 申购赎回清单(PCF) 抓取与解析。
================================================================
**职责（2026-09-16 重新表述，取代旧的"与 iopv_pcf 互不相干"）**：

  取数 + 解析层（本模块）：深交所 `ETF{code}{YYYYMMDD}.txt`（GBK 宽表）。
      对外只暴露纯函数：`fetch_pcf()` / `parse_pcf()` / `parse_pcf_txt()` / `parse_pcf_xml()`。
  落库层（唯一出口）：`core/iopv_pcf.PCFFetcher._persist()` —— 两个市场共用一套
      `pcf_snapshot` + `pcf_component` 写入口（避免"有 snapshot 无成分"的半残行）。
  依赖方向单向：`iopv_pcf` → 本模块的纯解析函数；本模块 **不 import** iopv_pcf、不写库。

  ⇒ 红线本意是"**不出现两套落库 / 两套口径**"，不是"禁止函数复用"。

数据源优先级（自动尝试）：
  1) 深交所静态 CDN 文本清单
        https://reportdocs.static.szse.cn/files/text/etf/ETF{c}{d}.txt
     —— 真实可用路径（沙箱/本机均可直连，txt 宽表、GBK 编码）。
     🔴 **历史可回溯到上市首日**（159366 实测 2025-04-14 起每个交易日均在；非交易日 404）。
  2) 深交所 PCF 直链（旧式 xml，常被限流，仅兜底）
        www.szse.cn/disclosure/fund/etf/pcf_{c}_{d}.xml 等多 path 候选
  3) 深交所 ShowReport 列表接口（CATALOGID=sgshqd）拿附件 URL
  4) 手动 --url / --file 喂浏览器 F12 抓到的真实链接或本地文件

重要边界（跨境 FoF 如 159687）：
  其 PCF 成分含虚拟现金证券 159900 + 境外 ETF 份额(如 LCU=CSOP SGX ETF)，
  无 A 股盘口，自算 IOPV 覆盖率 <50% 会触发 iopv_engine 的"不可信"闸门。
  本模块只负责"取清单元信息"（最小申赎单位/现金差额/申赎状态/替代比例/成分明细），
  精确套利定价需成分行情，那是后一步且对 FoF 天然受限。
"""
import sys
import os
import re
import json
import argparse
import xml.etree.ElementTree as ET

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(CORE_DIR), "data", "pcf")
os.makedirs(DATA_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
def _http_get(url, ref=None, timeout=15):
    """返回原始 bytes（保留编码信息，调用方自行解码）。"""
    import urllib.request
    import gzip
    h = {"User-Agent": UA, "Accept": ACCEPT, "Accept-Language": "zh-CN,zh;q=0.9"}
    if ref:
        h["Referer"] = ref
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw


def _decode(raw):
    """深交所 PCF 有 utf-8(xml) 与 GBK(txt宽表) 两种编码，自动探测。"""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _candidate_urls(etf_code, date):
    # 1) 真实可用路径：静态 CDN 文本清单（优先）
    cdn = "https://reportdocs.static.szse.cn/files/text/etf/ETF{c}{d}.txt"
    # 2) 兜底：旧式 xml 直链（常被限流）
    xml_candidates = [
        "https://www.szse.cn/disclosure/fund/etf/pcf_{c}_{d}.xml",
        "https://www.szse.cn/www/disclosure/fund/etf/pcf_{c}_{d}.xml",
        "http://www.szse.cn/disclosure/fund/etf/pcf_{c}_{d}.xml",
        "https://res.szse.cn/disclosure/fund/etf/pcf_{c}_{d}.xml",
    ]
    return [cdn.format(c=etf_code, d=date)] + \
        [t.format(c=etf_code, d=date) for t in xml_candidates]


def _looks_like_pcf(text):
    if not text:
        return False
    low = text.lower()
    return (("<pcf" in low)
            or ("creationredemptionunit" in low)
            or ("申购赎回清单" in text)
            or ("组合信息内容" in text)
            or ("现金差额" in text))


def _is_xml(text):
    t = text.lstrip()
    return t.startswith("<") or "<pcf" in text.lower() or "creationredemption" in text.lower()


def _find_pcf_link(report_json, date):
    """从 ShowReport 返回的 JSON/HTML 里抠出 pcf xml 附件链接。"""
    if not report_json:
        return None
    for m in re.finditer(r'(https?://[^\s"\'<>]+?pcf_[0-9]+_%s\.xml)' % date, report_json):
        return m.group(1)
    for m in re.finditer(r'(pcf_[0-9]+_%s\.xml)' % date, report_json):
        return "https://www.szse.cn/" + m.group(1)
    return None


def fetch_pcf(etf_code, date):
    """返回 (text, source_url)。全自动尝试直链(CDN txt 优先)与 ShowReport。"""
    # 1) 直链（CDN txt 置顶）
    for url in _candidate_urls(etf_code, date):
        try:
            raw = _http_get(url, ref="https://www.szse.cn/www/disclosure/fund/etf/index.html")
            txt = _decode(raw)
            if _looks_like_pcf(txt):
                return txt, url
        except Exception:
            continue
    # 2) ShowReport 列表接口
    try:
        api = ("https://www.szse.cn/api/report/ShowReport?SHOWTYPE=JSON&"
               "CATALOGID=sgshqd&TABKEY=tab1&txtQuery=%s&PAGENO=1" % etf_code)
        js = _decode(_http_get(api, ref="https://www.szse.cn/www/disclosure/fund/etf/index.html"))
        link = _find_pcf_link(js, date)
        if link:
            raw = _http_get(link, ref="https://www.szse.cn/www/disclosure/fund/etf/index.html")
            txt = _decode(raw)
            if _looks_like_pcf(txt):
                return txt, link
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# 解析：XML（旧式，兜底）
# ---------------------------------------------------------------------------
def _norm_code(code, source):
    """深交所 102→.SZ，沪市 103→.SH；北交所 104→.BJ；港股(5 位)、境外(OS) 原样返回。

    ⚠️ 2026-09-16 修：跨境 ETF 的港股 5 位码（如 00241 阿里健康）此前会因
    前缀 "0" 被误加 `.SZ` 后缀（`_norm_code('00241','')` → '00241.SZ'）。
    港股行情口是 `hk00241`，带后缀就取不到价 —— 故 5 位码一律原样返回。

    ⚠️ 2026-09-17 修：境外成分（`src='OS'`）**一律原样返回**。美股字母码（AAPL）本就
    不含 `0/1/2/3` 前缀不会中招，但**日股/韩股的纯数字码会**（4 位 `1330` → `1330.SZ`）。
    境外码用不到 A 股后缀，加了只会污染。"""
    code = (code or "").strip()
    if not code or "." in code:
        return code
    src = str(source).strip()
    if src in ("HK", "H"):
        # 🔴 2026-09-17：港股码统一 **5 位补零** 落库。沪市 PCF 给不定长码（`1093` / `13`），
        #   深市给已补零码（`00241`）⇒ 统一形态后，库里不再有「同一种码两种写法」。
        #   行情层（hk_quote._hk_code）同样 zfill(5)，双重保险。
        return code.zfill(5) if code.isdigit() else code
    if src == "OS" or (not src and len(code) == 5):
        return code
    if src == "102" or code.startswith(("0", "3", "2", "1")):
        return code + ".SZ"
    if src == "103" or code.startswith("6"):
        return code + ".SH"
    if src == "104":
        return code + ".BJ"
    return code


def _fnum(el, default=0.0):
    if el is not None and el.text:
        try:
            return float(el.text.strip())
        except ValueError:
            return default
    return default


def _itxt(el):
    return el.text.strip() if (el is not None and el.text) else ""


def _first(*els):
    """返回第一个非 None 的元素（避免 Element 在 or 链里的真值歧义）。"""
    for e in els:
        if e is not None:
            return e
    return None


def parse_pcf_xml(xml_text, etf_code):
    """把深交所 PCF XML 解析为结构化 dict（对齐 iopv_engine.ETFInfo）。"""
    root = ET.fromstring(xml_text)

    def find1(*tags):
        for t in tags:
            el = root.find(t)
            if el is not None:
                return el
        return None

    unit_el = find1("CreationRedemptionUnit", "CreationRedemption")
    cash_est = find1("EstimateCashComponent", "CashEstimate")
    cash_t1 = find1("CashComponent")
    nav_cu = find1("NAVperCU", "NAVPerCU")
    create_el = find1("Creation")
    redeem_el = find1("Redemption")
    rec_num = find1("RecordNum")
    fund_name = find1("FundName", "SecurityName", "ETFName")

    info = {
        "etf_code": etf_code,
        "etf_name": _itxt(fund_name),
        "report_unit": _fnum(unit_el),
        "cash_balance": _fnum(cash_est),
        "cash_balance_t1": _fnum(cash_t1),
        "nav_per_cu": _fnum(nav_cu),
        "enable_creation": _itxt(create_el) in ("1", "true", "True", "Y", "是"),
        "enable_redemption": _itxt(redeem_el) in ("1", "true", "True", "Y", "是"),
        "record_num": int(_fnum(rec_num)),
        "components": [],
    }

    recs = root.find("Records") or root.find("Components") or root.find("records")
    rec_nodes = []
    if recs is not None:
        rec_nodes = recs.findall("Record") or recs.findall("Component") or recs.findall("record")
    if not rec_nodes:
        rec_nodes = root.findall("Record") or root.findall("Component")

    for rec in rec_nodes:
        sid = _itxt(_first(rec.find("SecurityID"), rec.find("Code"), rec.find("securityID")))
        sname = _itxt(_first(rec.find("SecurityName"), rec.find("Name"), rec.find("securityName")))
        src = _itxt(_first(rec.find("SecurityIDSource"), rec.find("IDSource")))
        qty = _fnum(_first(rec.find("Quantity"), rec.find("Volume")))
        cf_txt = _itxt(_first(rec.find("CashFlag"), rec.find("ReplaceFlag"), rec.find("cashFlag")))
        try:
            cash_flag = int(float(cf_txt)) if cf_txt not in ("", None) else 0
        except ValueError:
            cash_flag = 0
        prem = _fnum(_first(rec.find("PremiumRatio"), rec.find("Premium")))
        ra = _fnum(_first(rec.find("ReplaceAmount"), rec.find("ApplyReplaceAmount")))
        rra = _fnum(_first(rec.find("RedemptionReplaceAmount"), rec.find("RedeemReplaceAmount")))
        is_virtual = (sid == "159900")
        info["components"].append({
            "code": _norm_code(sid, src),
            "raw_code": sid,
            "name": sname,
            "source": src,
            "volume": qty,
            "replace_flag": cash_flag,
            "replace_premium": prem,
            "fixed_amount": ra,
            "fixed_amount_redemption": rra,
            "is_cross_market": src not in ("102", "103", "104"),
            "is_virtual": is_virtual,
        })

    real = [c for c in info["components"] if not c["is_virtual"]]
    info["real_component_count"] = len(real)
    return info


# ---------------------------------------------------------------------------
# 解析：TXT 宽表（深交所实际给跨境ETF的格式，GBK）
# ---------------------------------------------------------------------------
def _extract_labeled(text):
    """把 '标签： 值' 形式的行抽成 {标签: 值}。"""
    out = {}
    pat = re.compile(r'([\u4e00-\u9fffA-Za-z（）()、,，.\d%]+?)\s*[:：]\s*(.+)')
    for line in text.splitlines():
        m = pat.search(line)
        if not m:
            continue
        label = m.group(1).strip()
        val = m.group(2).strip()
        if label and val:
            out[label] = val
    return out


def _to_float(v):
    """清洗 '173.83元' / '100.00%' / '12,455' / '2只(含"159900"证券)' -> 数值。
    提取首个数字序列（忽略单位/引号/括号干扰）；含 % 自动 /100。"""
    if v is None:
        return 0.0
    s = str(v).replace(",", "")
    m = re.search(r'-?\d+(\.\d+)?', s)
    if not m:
        return 0.0
    try:
        val = float(m.group(0))
    except ValueError:
        return 0.0
    return val / 100.0 if "%" in s else val


_FLAG_MAP = {"禁止": 0, "允许": 1, "必须": 2, "必须退补": 3}


def _parse_component_row(row):
    """解析 PCF 成分行。深交所宽表列间为不定空格，靠语义锚定而非固定列宽：
    行首=代码, 次=名称(可能含空格), 行尾中文=市场(深圳/上海/其他/**香港**市场),
    中文 flag(必须/允许/禁止/必须退补), 百分比=申购保证金率,
    纯数字 token 中首枚=股份数量, 市场前末两枚=申购/赎回替代金额。"""
    row = row.strip()
    toks = re.split(r'\s{2,}', row)
    code = toks[0] if toks else ""
    name = toks[1] if len(toks) > 1 else ""
    market = toks[-1] if (toks and re.search(r'(深圳市场|上海市场|其他市场|香港市场)$', toks[-1])) else ""
    flag = ""
    for f in ("必须退补", "必须", "允许", "禁止"):
        if f in row:
            flag = f
            break
    pm = re.search(r'(\d+(?:\.\d+)?)\s*%', row)
    buy_margin = (pm.group(1) + "%") if pm else ""
    num_toks = [t for t in toks[2:] if re.match(r'^[\d,]+(\.\d+)?$', t)]
    qty = num_toks[0] if num_toks else "0"
    buy_amt = sell_amt = "0"
    if len(num_toks) >= 3:
        buy_amt, sell_amt = num_toks[-2], num_toks[-1]
    elif len(num_toks) == 2:
        buy_amt, sell_amt = num_toks[0], num_toks[1]
    return code, name, qty, flag, buy_margin, buy_amt, sell_amt, market


def _int_or_none(v):
    """'20000000份' -> 20000000；'不设上限' -> None（不兜底造数）。"""
    m = re.search(r'(\d[\d,]*)\s*份', str(v or ""))
    return int(m.group(1).replace(",", "")) if m else None




def parse_pcf_txt(text, etf_code):
    """解析深交所 PCF 文本宽表（reportdocs.static.szse.cn 的 ETFxxxxYYYYMMDD.txt）。

    ⚠️ 深交所 txt **自带两个日期段**（关键，勿靠交易日历推算）：
        「2026-09-15日 信息内容」= T-1 段 → nav / cash_diff / unit_nav（篮子定价基准日 D）
        「2026-09-16日 信息内容」= T  段 → 预估现金差额 / 替代比例上限 / 申赎状态（公告日 T）
        ⇒ info['sec_date_pre'] / info['sec_date_trade']，落库时直接映射
          pre_trade_date / trade_date。
    """
    labeled = _extract_labeled(text)

    def b(v):  # 是否开放类布尔
        return str(v or "").startswith(("允许", "是"))

    secs = re.findall(r'(\d{4}-\d{2}-\d{2})\s*日\s*信息内容', text)

    info = {
        "etf_code": etf_code,
        "etf_name": labeled.get("基金名称", ""),
        "fund_company": labeled.get("基金管理公司名称", ""),
        "target_index": labeled.get("目标指数代码", ""),
        "fund_type": labeled.get("基金类型", ""),
        "sec_date_pre": secs[0] if len(secs) >= 1 else "",
        "sec_date_trade": secs[-1] if len(secs) >= 2 else "",
        "report_unit": _to_float(labeled.get("最小申购、赎回单位")),
        "cash_balance": _to_float(labeled.get("预估现金差额")),
        "cash_balance_t1": _to_float(labeled.get("现金差额")),
        "nav_per_cu": _to_float(labeled.get("最小申购、赎回单位资产净值")),
        "nav_per_unit": _to_float(labeled.get("基金份额净值")),
        "publish_iopv": b(labeled.get("是否需要公布IOPV")),
        "cash_replace_cap": _to_float(labeled.get("可以现金替代比例上限")),
        "enable_creation": b(labeled.get("是否开放申购")),
        "enable_redemption": b(labeled.get("是否开放赎回")),
        "creation_limit": _int_or_none(labeled.get("当天累计可申购的基金份额上限")),
        "redemption_limit": _int_or_none(labeled.get("当天累计可赎回的基金份额上限")),
        "local_component_count": int(_to_float(labeled.get("本市场申购赎回组合证券只数", 0))),
        "total_component_count": int(_to_float(labeled.get("全部申购赎回组合证券只数", 0))),
        "components": [],
    }

    # 组合信息内容：表头行 + 成分行（宽表列间为不定空格，按语义锚定解析）
    lines = text.splitlines()
    hidx = None
    for i, l in enumerate(lines):
        if "证券代码" in l and "证券简称" in l:
            hidx = i
            break
    if hidx is not None:
        for l in lines[hidx + 1:]:
            if set(l.strip()) <= set("-"):
                break
            if not l.strip() or "证券代码" in l:
                continue
            code, name, qty, flag, buy_margin, buy_amt, sell_amt, market = _parse_component_row(l)
            if not code:
                continue
            src = ("102" if market == "深圳市场"
                   else "103" if market == "上海市场"
                   else "HK" if market == "香港市场" else "")
            replace_flag = _FLAG_MAP.get(flag, 1 if flag == "允许" else 0)
            is_virtual = (code == "159900")
            info["components"].append({
                "code": _norm_code(code, src),
                "raw_code": code,
                "name": name,
                "source": src,
                "market": market,
                "volume": _to_float(qty),
                "replace_flag": replace_flag,
                "replace_premium": _to_float(buy_margin),
                "fixed_amount": _to_float(buy_amt),
                "fixed_amount_redemption": _to_float(sell_amt),
                "is_cross_market": market not in ("深圳市场", "上海市场"),
                "is_virtual": is_virtual,
            })

    real = [c for c in info["components"] if not c["is_virtual"]]
    info["real_component_count"] = len(real)
    return info


def parse_pcf(text, etf_code):
    """按内容分派到 xml / txt 解析器。"""
    if _is_xml(text):
        return parse_pcf_xml(text, etf_code)
    return parse_pcf_txt(text, etf_code)


# ---------------------------------------------------------------------------
# 缓存 / CLI
# ---------------------------------------------------------------------------
def save_pcf(info, date, source_url=""):
    fn = os.path.join(DATA_DIR, "pcf_%s_%s.json" % (info["etf_code"], date))
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"source_url": source_url, "date": date, "pcf": info},
                  f, ensure_ascii=False, indent=2)
    return fn


def main():
    ap = argparse.ArgumentParser(description="深交所 PCF 抓取与解析")
    ap.add_argument("etf_code", nargs="?", help="ETF 代码，如 159687")
    ap.add_argument("trade_date", nargs="?", help="交易日 YYYYMMDD，如 20260902")
    ap.add_argument("--url", help="直接喂浏览器 F12 抓到的真实 PCF URL")
    ap.add_argument("--file", help="直接喂本地 PCF 文件(xml 或 txt)")
    ap.add_argument("--today", action="store_true", help="用今天日期(YYYYMMDD)")
    args = ap.parse_args()

    if args.today and not args.trade_date:
        from datetime import datetime
        args.trade_date = datetime.now().strftime("%Y%m%d")

    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="ignore") as f:
            xml_text = f.read()
        src = args.file
    elif args.url:
        xml_text = _decode(_http_get(args.url, ref="https://www.szse.cn/"))
        src = args.url
    elif args.etf_code and args.trade_date:
        xml_text, src = fetch_pcf(args.etf_code, args.trade_date)
    else:
        ap.print_help()
        return

    if not xml_text or not _looks_like_pcf(xml_text):
        print("[失败] 未取到有效 PCF 内容。可改用 --url(浏览器F12抓真实链接) 或 --file(本地文件)。")
        print("  自动模式优先走 reportdocs.static.szse.cn 的 ETF{c}{d}.txt；")
        print("  若仍失败，多半是深交所对该 IP 限流，F12 抓真实 URL 最稳。")
        return

    info = parse_pcf(xml_text, args.etf_code or "UNKNOWN")
    date = args.trade_date or "unknown"
    fn = save_pcf(info, date, src)
    print("[OK] 解析完成 -> %s" % fn)
    print("  基金: %s (%s) | 公司: %s" % (info["etf_name"], info["etf_code"], info.get("fund_company", "")))
    print("  类型: %s | 目标指数: %s | 公布IOPV: %s" %
          (info.get("fund_type", ""), info.get("target_index", ""), info.get("publish_iopv")))
    print("  最小申赎单位: %s 份 | 每篮子净值(T-1): %.4f | 单位净值(T-1): %.4f" %
          (info["report_unit"], info["nav_per_cu"], info.get("nav_per_unit", 0.0)))
    print("  预估现金差额(ECC): %.2f 元 | T-1现金差额: %.2f 元 | 替代比例上限: %.2f%%" %
          (info["cash_balance"], info["cash_balance_t1"], info.get("cash_replace_cap", 0) * 100))
    print("  申购开放: %s | 赎回开放: %s" % (info["enable_creation"], info["enable_redemption"]))
    print("  成分数: %d (真实成分 %d, 虚拟现金证券 %d) | 本市场:%s 全部:%s" % (
        len(info["components"]), info["real_component_count"],
        sum(1 for c in info["components"] if c["is_virtual"]),
        info.get("local_component_count", 0), info.get("total_component_count", 0)))
    print("  成分明细:")
    for c in info["components"]:
        print("    %s %s 数量=%s 替代标志=%s 申购保证金率=%.4f 申购替代额=%.2f 市场=%s%s" % (
            c["code"], c["name"], c["volume"], c["replace_flag"],
            c["replace_premium"], c["fixed_amount"], c.get("market", ""),
            " [虚拟现金]" if c["is_virtual"] else (" [跨市场]" if c["is_cross_market"] else "")))


if __name__ == "__main__":
    main()
