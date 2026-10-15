# -*- coding: utf-8 -*-
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import urllib.request, ssl
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0"
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("utf-8", "ignore")

for sym in ["hkHSCI", "hkHSI"]:
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,2025-01-01,20500101,400," % sym
    try:
        j = json.loads(get(url)); node = (j.get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        print("%s -> rows=%d  first=%s  last=%s" % (sym, len(arr), arr[0] if arr else None, arr[-1] if arr else None))
    except Exception as e:
        print("%s ERR %s" % (sym, e))
