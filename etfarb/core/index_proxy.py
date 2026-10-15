# -*- coding: utf-8 -*-
"""指数代理取价 —— 「单券 FoF / 境外成分」型跨境 ETF 的估值通道。

适用场景
--------
整篮**拿不到逐券实时行情**的标的：
  · 单券 FoF：`513000` → 东京上市的 `1330`（上場インデックスファンド225）378 股
    （腾讯 `jp1330` 空、新浪无日股个股 ⇒ 无实时价）；同构：`159687` → 新加坡 `LCU`。
  · 含境外成分：美股/日股/韩股 —— A 股时段**与当地交易时段不重叠**，成分价是冻结常量。

原理
----
逐券法的 IOPV 是 `Σ(股数 × 现价 × 汇率)`。取不到成分价时改用**比值法**：

    篮子货值(T) ≈ 篮子货值(D) × [指数(T) / 指数(D)] × [汇率(T) / 汇率(D)]

· 成立前提：该成分**实物复制**某公开指数 ⇒ 成分价 ÷ 指数 = 常数，日内跟踪误差可忽略；
  残差主要来自成分自身折溢价。
· 锚点 `篮子货值(D)` **不需要成分明细**：直接取官方 PCF 的
  `NAVperCU(D) − PreCashComponent(D)`（513000 实测与 `Σ SubstitutionCashAmount` 分毫一致）。
· 两个比值都来自「现价 + 昨收」的**同一次响应** ⇒ **不需要任何历史接口**。

🔴 指数源（2026-09-17 换源，旧的 `int_*` 已死）
------------------------------------------------
**新浪国际指数 `int_nikkei` / `int_sp500` / `int_nasdaq` 是死数据**（停在约 2025-09）：
`int_nikkei` 报 44946，真实日经 64136（差 30%）；`int_sp500` 报 6644，真实 7552。
⇒ **禁用**，改用下表（全部免费无鉴权，本轮逐个实测通过）：

| 源 | 覆盖 | 取法 |
|---|---|---|
| 腾讯 `qt.gtimg.cn` | 美股 `usINX/usNDX/usIXIC/usDJI`、港股 `hkHSI/hkHSTECH/hkHSCEI` | `[3]`=现价 `[4]`=昨收 |
| 东财 `push2delay` | 日经/德/法/英/韩/巴西/沙特/澳/新/印… | `secid=100.XXX`，`f43`/`f60` **÷100** |

实测校验（关键）：腾讯 `usINX` 的 `[3]/[4]` = 7551.81/7585.73 = **−0.447%**，与集思录
欧美表「T-1指数涨幅 −0.45%」**吻合** ⇒ 证实 `[3]/[4]` 就是集思录同口径的比值。

🔴 为什么两组用同一个 `last/prev` 就够（集思录也是这样）：
  · 亚洲（港股/日经，与 A 股时段重叠）：`last` = T 日实时、`prev` = T-1 收盘 ⇒ 正是 D=T-1 → T。
  · 欧美（美股，A 股 T 日盘中其最新收盘是 T-1）：`last` = T-1 收盘、`prev` = T-2 收盘 ⇒ 正是 D=T-2 → T-1。
  两种情形下 `last/prev` 都恰好是「D → 可用最新」的比值。

🔴 本模块只吐**比值**，不落库、不兜底；取不到即抛 `IndexProxyMissing`，
   由调用方按红线暴露错误（绝不退回表观值）。
"""
import json
import re
import ssl
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SINA_REF = "https://finance.sina.com.cn/"


class IndexProxyMissing(Exception):
    """指数代理取价缺失（指数或汇率取不到）。绝不兜底。"""
    pass


# ----------------------------------------------------------------------
# 指数登记表：key -> (源, 代码, 显示名)
#   · 'tencent'：`qt.gtimg.cn`（美股 us* / 港股 hk*），[3]=现价 [4]=昨收（**不除权**）
#   · 'em'     ：东财 `push2delay`，secid = `100.<代码>`，f43/f60 **÷100**
# ----------------------------------------------------------------------
INDEX_CODES = {
    # ---- 日本 ----
    "N225":     ("em",      "100.N225",     "日经225"),
    # `100.TOPIX` / `100.TPX` 东财**均无数据**（2026-09-17 实测）⇒ 东证指数暂缺，
    # 唯一标的 513800（成交额 3235 万）待 P3 找源（或退化为「用日经代」不可接受）。
    # ---- 韩国 ----
    "KS11":     ("em",      "100.KS11",     "韩国KOSPI"),
    "KOSPI200": ("em",      "100.KOSPI200", "韩国KOSPI200"),
    # ---- 美洲 ----
    "SP500":    ("tencent", "usINX",        "标普500"),
    "NDX":      ("tencent", "usNDX",        "纳斯达克100"),
    "NASDAQ":   ("tencent", "usNDX",        "纳斯达克100"),   # 旧键名兼容
    "IXIC":     ("tencent", "usIXIC",       "纳斯达克"),
    "DJI":      ("tencent", "usDJI",        "道琼斯"),
    "BVSP":     ("em",      "100.BVSP",     "巴西BOVESPA"),
    # ---- 欧洲 ----
    "DAX":      ("em",      "100.GDAXI",    "德国DAX30"),
    "CAC":      ("em",      "100.FCHI",     "法国CAC40"),
    "FTSE":     ("em",      "100.FTSE",     "英国富时100"),
    "SX5E":     ("em",      "100.SX5E",     "欧洲斯托克50"),
    "MIB":      ("em",      "100.MIB",      "富时意大利MIB"),
    # ---- 港股（亚洲组的恒指系；也可作代理指数）----
    "HSI":      ("tencent", "hkHSI",        "恒生指数"),
    "HSTECH":   ("tencent", "hkHSTECH",     "恒生科技"),
    "HSCEI":    ("tencent", "hkHSCEI",      "国企指数"),
    # ---- 其他 ----
    "SAUDI":    ("em",      "100.FISAULMU", "富时沙特阿拉伯"),
    "SENSEX":   ("em",      "100.SENSEX",   "印度孟买SENSEX"),
    "AS51":     ("em",      "100.AS51",     "澳洲标普200"),
    "STI":      ("em",      "100.STI",      "新加坡海峡时报"),
}

# 新浪外汇：逗号分隔，[1]=现价、[3]=昨收
#   校验方式：`(f[1]-f[3])/f[3]` 应等于 f[10]（涨跌幅），513000 实测 -0.0463% 吻合
FX_CODES = {
    "JPY/CNY": "fx_sjpycny",
    "USD/CNY": "fx_susdcny",
    "HKD/CNY": "fx_shkdcny",
}


class IndexProxyQuote:
    """「指数比 × 汇率比」取价器。无缓存、不落库（行情红线）。"""

    # ---- 低层取数 ----
    def _get(self, url, ref="https://finance.sina.com.cn/", enc="gbk"):
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": ref})
        return urllib.request.urlopen(req, timeout=15, context=CTX).read().decode(enc, "replace")

    def _get_tencent_index(self, code):
        """腾讯指数 → (last, prev, name)。美股 `usINX` / 港股 `hkHSI`，均 [3]=现价 [4]=昨收。"""
        raw = self._get("https://qt.gtimg.cn/q=" + code, ref="https://finance.qq.com/", enc="gbk")
        m = re.search(r'="([^"]*)"', raw)
        if not m or not m.group(1).strip():
            raise IndexProxyMissing("腾讯指数 %s 无返回（空响应）" % code)
        f = m.group(1).split("~")
        if len(f) < 5:
            raise IndexProxyMissing("腾讯指数 %s 字段不足: %r" % (code, f[:6]))
        try:
            last, prev = float(f[3]), float(f[4])
        except ValueError:
            raise IndexProxyMissing("腾讯指数 %s 数值解析失败: %r" % (code, f[:6]))
        return last, prev, (f[1] or code)

    def _get_em_index(self, secid):
        """东财国际指数 → (last, prev, name)。`f43`=现价 `f60`=昨收，**均 ÷100**。"""
        url = ("https://push2delay.eastmoney.com/api/qt/stock/get?secid=%s"
               "&fields=f43,f58,f60,f170" % secid)
        try:
            d = (json.loads(self._get(url, ref="https://quote.eastmoney.com/", enc="utf-8"))
                 or {}).get("data")
        except Exception as e:
            raise IndexProxyMissing("东财指数 %s 请求失败: %s" % (secid, e))
        if not d:
            raise IndexProxyMissing("东财指数 %s 无数据（secid 可能不对）" % secid)
        last, prev = d.get("f43"), d.get("f60")
        if last in (None, "-", "") or prev in (None, "-", ""):
            raise IndexProxyMissing("东财指数 %s 缺 f43/f60: %r" % (secid, d))
        try:
            return float(last) / 100.0, float(prev) / 100.0, (d.get("f58") or secid)
        except (TypeError, ValueError):
            raise IndexProxyMissing("东财指数 %s 数值解析失败: %r" % (secid, d))

    # ---- 对外 ----
    def get_index_ratio(self, key="N225"):
        """返回 {name,last,prev,ratio,source,code}。ratio = 现价 ÷ 昨收。

        `last/prev` 的语义对亚洲/欧美两组**都正确**（见模块 docstring）。"""
        ent = INDEX_CODES.get(key)
        if not ent:
            raise IndexProxyMissing("未登记的指数键: %s（已登记 %s）"
                                    % (key, sorted(INDEX_CODES)))
        source, code, name = ent
        if source == "tencent":
            last, prev, nm = self._get_tencent_index(code)
            src_tag = "tencent"
        elif source == "em":
            last, prev, nm = self._get_em_index(code)
            src_tag = "eastmoney"
        else:
            raise IndexProxyMissing("未支持的指数源: %r（键 %s）" % (source, key))
        if not prev:
            raise IndexProxyMissing("指数 %s 昨收为 0，无法取比" % code)
        return {"name": nm or name, "code": code, "last": last, "prev": prev,
                "ratio": last / prev, "source": src_tag}

    def get_fx_ratio(self, pair="JPY/CNY"):
        """返回 {last,prev,ratio,source,code}。ratio = 现价 ÷ 昨收。"""
        code = FX_CODES.get(pair)
        if not code:
            raise IndexProxyMissing("未登记的外汇对: %s（可选 %s）" % (pair, list(FX_CODES)))
        parts = self._get("https://hq.sinajs.cn/list=" + code).split(",")
        if len(parts) < 4:
            raise IndexProxyMissing("新浪外汇 %s 字段不足: %r" % (code, parts[:6]))
        try:
            last, prev = float(parts[1]), float(parts[3])
        except ValueError:
            raise IndexProxyMissing("新浪外汇 %s 数值解析失败: %r" % (code, parts[:5]))
        if not prev:
            raise IndexProxyMissing("新浪外汇 %s 昨收为 0，无法取比" % code)
        return {"last": last, "prev": prev, "ratio": last / prev,
                "source": "sina_fx", "code": code}

    def get_basket_scaler(self, index_key, fx_pair):
        """篮子缩放因子 = 指数比 × 汇率比。返回 dict（含各自明细，便于上层展示与审计）。"""
        idx = self.get_index_ratio(index_key)
        fx = self.get_fx_ratio(fx_pair)
        return {
            "scaler": idx["ratio"] * fx["ratio"],
            "index": idx,
            "fx": fx,
        }


def main():
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    q = IndexProxyQuote()
    print("=== 指数源实测（全部登记项）===")
    for k in INDEX_CODES:
        try:
            r = q.get_index_ratio(k)
            print("  %-9s %-14s last=%-12.2f prev=%-12.2f ratio=%+.4f%%  [%s %s]"
                  % (k, r["name"], r["last"], r["prev"], (r["ratio"] - 1) * 100,
                     r["source"], r["code"]))
        except Exception as e:
            print("  %-9s ❌ %s" % (k, e))
    print()
    print("=== 汇率源实测 ===")
    for p in FX_CODES:
        try:
            r = q.get_fx_ratio(p)
            print("  %-8s last=%.6f prev=%.6f ratio=%+.4f%%" % (p, r["last"], r["prev"], (r["ratio"] - 1) * 100))
        except Exception as e:
            print("  %-8s ❌ %s" % (p, e))


if __name__ == "__main__":
    main()
