# -*- coding: utf-8 -*-
import sys, io, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import urllib.request, ssl
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("utf-8", "ignore")

# 腾讯港股指数日K线
for sym in ["hkHSCI", "hkHSI", "hkHSCEI"]:
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,40,qfq" % sym
    try:
        raw = get(url)
        j = json.loads(raw)
        node = (j.get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        print("%s -> tencent rows=%d  last=%s" % (sym, len(arr), arr[-1] if arr else None))
    except Exception as e:
        print("%s -> ERR %s" % (sym, e))

# 对照：东财 100.HSCI
url2 = "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=100.HSCI&klt=101&fqt=0&fields1=f1,f2,f3&fields2=f51,f53&beg=20240101&end=20500101"
try:
    raw = get(url2)
    j = json.loads(raw)
    kl = ((j.get("data") or {}).get("klines")) or []
    print("em 100.HSCI -> rows=%d" % len(kl))
except Exception as e:
    print("em 100.HSCI -> ERR %s" % e)
