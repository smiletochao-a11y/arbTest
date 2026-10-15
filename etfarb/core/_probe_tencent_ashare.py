# -*- coding: utf-8 -*-
"""探测：腾讯 fqkline 对 A 股指数日K是否可用 + 东财 push2his 加 Referer 是否复活。"""
import urllib.request, ssl, json

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def get(url, ref=None, timeout=15):
    hdr = {"User-Agent": UA}
    if ref:
        hdr.update(ref)
    req = urllib.request.Request(url, headers=hdr)
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")

print("=== A. Tencent fqkline A-share index daily ===")
for sym in ["sz399997", "sh000922", "sz399001", "sz399975", "sh000961", "sz399300"]:
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,10,day" % sym)
        raw = get(url)
        obj = json.loads(raw)
        node = (obj.get("data") or {}).get(sym) or {}
        days = node.get("day") or []
        if days:
            print("%-10s OK rows=%d first=%s last=%s close_last=%s" % (
                sym, len(days), days[0][0], days[-1][0], days[-1][2]))
        else:
            print("%-10s EMPTY keys=%s" % (sym, list(node.keys())[:6]))
    except Exception as e:
        print("%-10s EXC %r" % (sym, e))

print("=== B. EM push2his with Referer ===")
for secid in ["2.399997", "2.000922"]:
    for ref in [None, "https://quote.eastmoney.com/"]:
        try:
            url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
                   "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg=20250101&end=20500101" % secid)
            raw = get(url, ref={"Referer": ref} if ref else None)
            d = (json.loads(raw).get("data") or {})
            kl = d.get("klines") or []
            print("secid=%s ref=%s rows=%d %s" % (secid, (ref or "none")[:28], len(kl),
                  (kl[-1].split(",")[0] if kl else "-")))
        except Exception as e:
            print("secid=%s ref=%s EXC %r" % (secid, (ref or "none")[:28], e))
