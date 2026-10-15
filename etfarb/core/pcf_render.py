#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/pcf_render.py  —  交易所 PCF 原文件的「可读化」渲染（**只读**，不落库、不参与估值）

存在理由（东哥 2026-09-28）：
  主看板「PCF」列沪市标的走**上交所下载口**，该口响应头带
    `Content-Type: application/octet-stream`
    `Content-Disposition: attachment; filename="ssepcf_{code}_{date}.xml"`
  ⇒ 浏览器只会另存文件；改走本机代理后若原样吐 XML（513500 = 205 KB / 503 个
  `<Component>` 标签堆）**可读性极差** ⇒ 本模块把上交所 XML 渲染成一张
  自包含 HTML（关键字段 + 额度 + 成分表），一眼能看。

🔴 口径红线：
  · 本模块**不是数据源、不是落库口**。落库仍唯一走 `core/iopv_pcf.py:PCFFetcher._persist()`。
  · **只做呈现，不改任何数值**：页面上的数字与 XML 原文逐字一致；单位一律显式标注。
  · 沪市下发口径 = **累计**（`CreationLimit` / `CreationLimitPerAcct`），
    与深市下发的**净**（当天净申购上限）语义不同 ⇒ 本模块只服务沪市 XML，**不跨市场套用**。
  · 「≈N 个篮子」只是 `额度 ÷ 最小申赎单位` 的辅助换算，显式标 ≈，不当结论用。

用法：
  from pcf_render import fetch_sse_xml, render_sse_html
  html_doc = render_sse_html("513500", fetch_sse_xml("513500"))
"""
import html
import urllib.request
import xml.etree.ElementTree as ET

SSE_URL = "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={code}"
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"}

# 🔴 沪市 PCF `<UnderlyingSecurityID>` → 市场归属（与 core/iopv_pcf.py:PCFFetcher._SSE_MARKET 同源）
_SSE_MARKET = {"101": "上海", "102": "深圳", "103": "港股通", "104": "港股通",
               "106": "上海", "9999": "境外"}
# 现金替代标志（沪市 SubstitutionFlag）
_SUB_FLAG = {0: "禁止", 1: "允许", 2: "必须", 3: "必须退补"}
# 申赎开关 / 模式（沪市枚举）
_SWITCH = {"0": "暂停", "1": "开放"}
_MECHANISM = {"0": "现金申赎", "1": "实物申赎"}

# 关键字段表：(XML 标签, 中文标签, 单位/格式, 是否重点高亮)
#   fmt: "int" | "money" | "ratio" | "date" | "raw" | "intro"
_FIELDS = [
    ("FundInstrumentID", "基金代码", "raw", False),
    ("TradingDay", "公告日（TradingDay）", "date", True),
    ("PreTradingDay", "信息内容日 / 篮子定价基准日（PreTradingDay）", "date", True),
    ("CreationRedemptionUnit", "最小申购、赎回单位", "uint", True),
    ("NAVperCU", "最小申购赎回单位资产净值", "money", False),
    ("NAV", "基金份额净值", "money", False),
    ("PreCashComponent", "现金差额（PreCashComponent）", "money", False),
    ("EstimatedCashComponent", "预估现金差额（EstimatedCashComponent）", "money", False),
    ("MaxCashRatio", "可以现金替代比例上限", "ratio", False),
    ("CreationLimit", "当天累计可申购的基金份额上限", "int", True),
    ("RedemptionLimit", "当天累计可赎回的基金份额上限", "int", True),
    ("CreationLimitPerAcct", "单个证券账户累计申购上限", "int", True),
    ("RedemptionLimitPerAcct", "单个证券账户累计赎回上限", "int", True),
    ("NetCreationLimit", "当天**净**申购的基金份额上限（如发布）", "int", True),
    ("NetCreationLimitPerAcct", "单个证券账户**净**申购上限（如发布）", "int", True),
    ("RecordNumber", "成分证券只数", "cnt", False),
    ("PublishIOPVFlag", "是否公布 IOPV", "intro", False),
    ("CreationRedemptionSwitch", "申赎开关", "switch", True),
    ("CreationRedemptionMechanism", "申赎模式", "mechanism", False),
]
_KNOWN = set(t for t, _, _, _ in _FIELDS)

_STYLE = """
*{box-sizing:border-box}
body{margin:0;background:#f5f6f8;color:#1f2328;
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:22px 18px 64px}
h1{font-size:21px;margin:0 0 4px;font-weight:600;letter-spacing:.2px}
h1 .nm{font-weight:400;color:#3c4149;margin-left:6px}
.sub{margin:0 0 4px;color:#6b7280;font-size:13px}
.sub b{color:#1f2328;font-weight:600}
.src{margin:0 0 18px;color:#8a9199;font-size:12px}
.src a{color:#0b6bcb;text-decoration:none}
.src a:hover{text-decoration:underline}
.card{background:#fff;border:1px solid #e4e7eb;border-radius:10px;margin:0 0 16px;overflow:hidden}
.card>h2{margin:0;padding:11px 14px;font-size:14px;font-weight:600;background:#fafbfc;
 border-bottom:1px solid #e4e7eb;color:#2b3038}
.card>h2 span{font-weight:400;color:#8a9199;font-size:12px;margin-left:6px}
table{width:100%;border-collapse:collapse}
th,td{padding:7px 14px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
tr:last-child>td,tr:last-child>th{border-bottom:none}
.kv th{width:330px;font-weight:400;color:#4b5158;background:#fcfcfd;font-size:13px}
.kv td{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
 font-size:13.5px;color:#111418}
.kv tr.hi td{font-weight:700;color:#0b4fa8}
.kv tr.hi th{color:#0b4fa8}
.muted{color:#a6adb5;font-weight:400}
.note{color:#8a9199;font-size:12px;font-family:inherit;margin-left:6px}
.scroll{max-height:72vh;overflow:auto}
.scroll thead th{position:sticky;top:0;z-index:2;background:#f0f2f5;font-weight:600;font-size:12.5px;
 color:#3c4149;border-bottom:1px solid #dfe3e8;white-space:nowrap}
.scroll td{font-size:12.8px;white-space:nowrap}
.scroll td.n,.scroll th.n{text-align:right;font-variant-numeric:tabular-nums;
 font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.scroll tbody tr:nth-child(even){background:#fbfcfd}
.scroll tbody tr:hover{background:#f2f7fd}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11.5px;line-height:17px;
 background:#eef1f4;color:#4b5158}
.tag.ok{background:#e7f4ea;color:#1a7f37}
.tag.no{background:#fdecec;color:#c0392b}
.tag.hk{background:#eaf1fd;color:#0b4fa8}
footer{color:#a6adb5;font-size:12px;line-height:1.8;margin-top:18px}
code{background:#eef1f4;border-radius:3px;padding:0 4px;font-size:12px}
"""


# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------
def fetch_sse_xml(code, timeout=20):
    """抓上交所 PCF（最新一份）。**原样返回 bytes**，不做任何解码/加工。

    上游对非沪市代码返 HTTP 200 + 105 字节错误模板 `({"jsonCallBack":…})`，
    对当天未发布返回空 —— 两种情况都判为不可用（ValueError），绝不兜底造数。"""
    url = SSE_URL.format(code=str(code).strip().upper())
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if len(raw) < 512 or raw[:2] == b"({":
        raise ValueError("交易所未返回有效 PCF（可能尚未发布）: %d bytes" % len(raw))
    return raw


def decode_xml(raw):
    """沪市 XML 是 UTF-8；容错回退 gb18030（不丢字）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------
def _e(v):
    return html.escape(str(v if v is not None else ""))


def _fmt(v, kind, unit_sum=None):
    """格式化一个字段值（**只改显示形态，不改数值**）。"""
    s = str(v).strip() if v is not None else ""
    if s == "":
        return '<span class="muted">—</span>'
    if kind == "int":
        try:
            n = int(float(s))
        except ValueError:
            return _e(s)
        out = "{:,}".format(n)
        if unit_sum and unit_sum > 0:
            k = n / unit_sum
            out += '<span class="note">份%s</span>' % (
                "　≈%.2f 个篮子" % k if k >= 0.01 else "")
        else:
            out += '<span class="note">份</span>'
        return out
    if kind == "uint":
        try:
            return "{:,}<span class=\"note\">份</span>".format(int(float(s)))
        except ValueError:
            return _e(s)
    if kind == "cnt":
        try:
            return "{:,}<span class=\"note\">只</span>".format(int(float(s)))
        except ValueError:
            return _e(s)
    if kind == "money":
        try:
            f = float(s)
        except ValueError:
            return _e(s)
        # 🔴 保留原文小数位（NAV=2.4602 不能被截成 2.46），下限 2 位、上限 6 位
        dec = len(s.split(".", 1)[1]) if "." in s else 0
        dec = max(2, min(dec, 6))
        return ("{:,.%df}" % dec).format(f) + '<span class="note">元</span>'
    if kind == "ratio":
        try:
            return "{:.2f}%".format(float(s) * 100)
        except ValueError:
            return _e(s)
    if kind == "date":
        return _e(s[:4] + "-" + s[4:6] + "-" + s[6:8]) if len(s) == 8 and s.isdigit() else _e(s)
    if kind == "switch":
        lab = _SWITCH.get(s, s)
        cls = "ok" if s == "1" else "no"
        return '<span class="tag %s">%s</span> <span class="note">(%s)</span>' % (cls, _e(lab), _e(s))
    if kind == "mechanism":
        return '%s <span class="note">(%s)</span>' % (_e(_MECHANISM.get(s, s)), _e(s))
    if kind == "intro":
        lab = {"0": "否", "1": "是"}.get(s, s)
        return _e(lab) + '<span class="note">(%s)</span>' % _e(s)
    return _e(s)


def render_sse_html(code, raw, name_hint=""):
    """把上交所 PCF XML 渲成自包含 HTML。

    `raw`       可为 bytes（原文）或 str。
    `name_hint` 沪市 XML **不含基金名称** ⇒ 由调用方（后端查 fund_profile.fund_name）补，
                仅用于标题显示，不参与任何数值。"""
    text = decode_xml(raw) if isinstance(raw, (bytes, bytearray)) else str(raw)
    root = ET.fromstring(text)

    def g(tag):
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    code = g("FundInstrumentID") or str(code).strip().upper()
    trade_date = g("TradingDay")
    pre_trade_date = g("PreTradingDay")
    name = g("FundName") or g("SecurityName") or g("ETFName") or (name_hint or "")

    try:
        unit = int(float(g("CreationRedemptionUnit") or 0)) or None
    except ValueError:
        unit = None

    # ---- 卡片1：关键字段 ----
    rows = []
    seen = set()
    for tag, label, kind, hi in _FIELDS:
        val = g(tag)
        seen.add(tag)
        cls = ' class="hi"' if hi else ""
        rows.append('<tr%s><th>%s</th><td>%s</td></tr>'
                    % (cls, _e(label), _fmt(val, kind, unit if kind == "int" else None)))
    # 兜底：XML 里出现但未登记在 _FIELDS 的标量字段（不猜语义，原样列出）
    extra = []
    for el in root:
        if el.tag in _KNOWN or el.tag in ("ComponentList",):
            continue
        if len(list(el)):
            continue
        extra.append('<tr><th>%s<span class="note">未登记字段</span></th><td>%s</td></tr>'
                     % (_e(el.tag), _fmt((el.text or "").strip(), "raw")))
    if extra:
        rows.append('<tr><th colspan="2" style="background:#fafbfc;color:#8a9199;'
                    'font-size:12px">XML 中另有未登记字段</th></tr>')
        rows.extend(extra)

    # ---- 卡片2：成分 ----
    comps = []
    cl = root.find("ComponentList")
    if cl is not None:
        comps = cl.findall("Component")
    crows = []
    for c in comps:
        def cg(t):
            e = c.find(t)
            return (e.text or "").strip() if e is not None else ""
        sid = cg("InstrumentID")
        flag = cg("SubstitutionFlag")
        try:
            flag_lab = _SUB_FLAG.get(int(float(flag)), flag)
        except ValueError:
            flag_lab = flag
        cls = "ok" if flag_lab == "允许" else ("no" if flag_lab == "禁止" else "")
        uid = cg("UnderlyingSecurityID")
        mkt = _SSE_MARKET.get(uid, uid)
        mcls = ' class="tag hk"' if mkt == "港股通" else ' class="tag"'

        def num(t):
            v = cg(t)
            if v == "":
                return '<td class="n"><span class="muted">—</span></td>'
            try:
                return '<td class="n">%s</td>' % "{:,.4f}".format(float(v)).rstrip("0").rstrip(".")
            except ValueError:
                return '<td class="n">%s</td>' % _e(v)

        crows.append(
            "<tr><td>%s</td><td>%s</td>%s%s%s%s<td><span%s>%s</span></td></tr>"
            % (_e(sid), _e(cg("InstrumentName")),
               num("Quantity"), num("CreationPremiumRate"), num("RedemptionDiscountRate"),
               num("SubstitutionCashAmount"),
               mcls, _e(mkt)))
    # 表头 + 列宽（比率列标「原文」，hover 说明未做单位换算 —— 不猜语义）
    _tip = ' title="交易所原文值（%s），未做单位换算"'
    heads = ('<tr><th>证券代码</th><th>证券简称</th>'
             '<th class="n" title="篮子内股份数量（Quantity）">股份数量</th>'
             '<th class="n"' + (_tip % "CreationPremiumRate") + '>申购溢价率</th>'
             '<th class="n"' + (_tip % "RedemptionDiscountRate") + '>赎回折价率</th>'
             '<th class="n" title="现金替代金额（SubstitutionCashAmount）">替代金额(元)</th>'
             '<th>市场</th></tr>')

    title = "%s %s 申购赎回清单 PCF（%s）" % (code, name, trade_date or "—")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><div class="wrap">
<h1>{code}<span class="nm">{name}</span></h1>
<p class="sub">申购赎回清单（PCF）　公告日 <b>{td}</b>　篮子定价基准日 <b>{ptd}</b></p>
<p class="src">来源：上海证券交易所官方 PCF 下载文件（<a href="{src}" rel="noopener">ssepcf_{code}_{td}.xml</a>，
点击该链接会触发下载；本页是它的可读化呈现，数值未做任何加工）</p>

<section class="card"><h2>基本信息与额度<span>沪市下发口径 = 累计</span></h2>
<table class="kv">{rows}</table></section>

<section class="card"><h2>成分证券<span>{ncomp} 只</span></h2>
<div class="scroll"><table><thead>{heads}</thead><tbody>{crows}</tbody></table></div></section>

<footer>本页由 ETFarb 渲染自交易所原始 PCF 文件，仅作可读化呈现，不构成任何投资建议。<br>
「≈N 个篮子」= 额度 ÷ 最小申赎单位，为辅助换算，非交易所原文口径。</footer>
</div></body></html>""".format(
        title=_e(title), style=_STYLE, code=_e(code), name=_e(name),
        td=_fmt(trade_date, "date"), ptd=_fmt(pre_trade_date, "date"),
        src=_e(SSE_URL.format(code=code)), rows="\n".join(rows), ncomp=len(comps),
        heads=heads, crows="\n".join(crows))


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "513500"
    doc = render_sse_html(c, fetch_sse_xml(c))
    out = sys.argv[2] if len(sys.argv) > 2 else "pcf_%s.html" % c
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print("wrote %s (%d bytes)" % (out, len(doc)))
