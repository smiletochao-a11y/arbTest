# -*- coding: utf-8 -*-
"""
future 后端服务：单文件 HTTP 服务，提供 JSON API + 托管前端静态页。

启动：python app.py
端口：8103

API 一览：
  GET /api/basis?variety=IC          基差看板（当前四合约，日线口径）
  GET /api/history?variety=IC&years=1  历史基差/年化序列 + 分位统计
  GET /api/move?variety=IC&years=1   移仓助手（合约对价差分析）
  GET /api/pair?near=IC2610&far=IC2611&years=1  单个合约对详情
  GET /api/realtime?variety=IC       单个品种实时行情（前端按需调用）
  GET /api/realtime_all              四个品种实时行情（首屏专用，1 次拿全）
  GET /api/meta                      品种清单 / 数据最新日期 / 是否允许一键更新
  POST /api/refresh                  后台抓取最新数据（仅本机开放，见 ALLOW_REFRESH）
  GET /api/refresh_status            查询抓取进度与日志
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
import traceback
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "core")
WEB_DIR = os.path.join(ROOT, "frontend")
sys.path.insert(0, CORE)

from config import VARIETIES, ENABLED_VARIETIES, DEFAULT_PORT
import engine
import data_source as ds
import realtime as rt
import ironore


def json_resp(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    # API 一律不缓存：数据是实时算的，缓存会让前端拿到过期的 allow_refresh / 行情
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def build_realtime_payload(variety, quotes=None, spot_q=None):
    """
    组装单个品种的实时行情 payload。

    quotes/spot_q 可由调用方传入（批量接口会一次性拉完四个品种再分发），
    不传则自己抓（单品种接口的用法）。

    抽成独立函数的原因：/api/realtime 和 /api/realtime_all 要共用同一套
    基差/年化计算逻辑，绝不能写两份 —— 写两份迟早会漂移，两处数字对不上。
    """
    cfg = VARIETIES.get(variety, {})
    syms = ds.get_active_symbols(variety)
    if quotes is None:
        quotes = rt.fetch_futures_quote(syms)
    if spot_q is None:
        spot_q = rt.fetch_index_quote(cfg.get("spot_code"))
    spot_price = spot_q["price"] if spot_q else None

    # 用实时价重算基差与年化
    qmap = {q["symbol"]: q for q in quotes}
    rows = []
    for s in syms:
        q = qmap.get(s)
        if not q or q["price"] is None:
            continue
        ltd = ds.symbol_last_trade_date(s)
        days_left = None
        if ltd:
            # 注意：必须用纯日期相减，不能用 datetime.now()（含时分秒会导致少算1天）
            _today = datetime.now().date()
            _ltd = datetime.strptime(ltd, "%Y-%m-%d").date()
            days_left = (_ltd - _today).days
        basis = None
        basis_pct = None
        annual = None
        if spot_price:
            basis = spot_price - q["price"]
            basis_pct = basis / spot_price * 100
            if days_left and days_left > 0:
                annual = basis_pct * 365 / days_left
        rows.append({
            "symbol": s,
            "price": q["price"],
            "change": q["change"],
            "change_pct": q["change_pct"],
            "pre_close": q["pre_close"],
            "hold": q["hold"],
            "basis": round(basis, 2) if basis is not None else None,
            "basis_pct": round(basis_pct, 2) if basis_pct is not None else None,
            "annual": round(annual, 2) if annual is not None else None,
            "days_left": days_left,
        })
    return {
        "ok": True, "variety": variety,
        "name": cfg.get("name", ""),
        "spot": spot_q,
        "rows": rows,
        "spot_percentile": engine.spot_percentile(variety, years=3, current_value=spot_price),
        "is_trading": rt.is_trading_time(),
        "is_live": bool(spot_price is not None and len(rows) > 0),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============ 数据更新（页面一键抓取，仅本机开放） ============
# 为什么必须用环境变量控制，而不能靠"请求来自 127.0.0.1"判断：
#   线上是 Cloudflare → ARM nginx → 127.0.0.1:8103 反代，
#   后端看到的**所有**请求都来自 127.0.0.1，这个判据完全无效。
#   而抓取要枚举 4 个品种从上市到次年的全部月份合约（实测 600 个），
#   逐个发 HTTP 请求，在 1 核 ARM 上要跑几分钟 —— 对读者开放等于自杀。
#   所以线上 systemd 不设 ALLOW_REFRESH（默认 0），接口直接 403；
#   本地 start.bat 设 ALLOW_REFRESH=1。
ALLOW_REFRESH = os.environ.get("ALLOW_REFRESH", "0") == "1"

_REFRESH_LOCK = threading.Lock()
_REFRESH_LOG_MAX = 400
_refresh = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "iron_rc": None,
    "new_rows": None,
    "error": None,
    "stage": None,      # 当前在跑哪一步（前端提示用）
    "log": [],
}


def _refresh_snapshot():
    """当前抓取状态快照。log 只回传尾部若干行，避免响应过大。"""
    with _REFRESH_LOCK:
        snap = {
            "running": _refresh["running"],
            "started_at": _refresh["started_at"],
            "finished_at": _refresh["finished_at"],
            "returncode": _refresh["returncode"],
            "iron_rc": _refresh["iron_rc"],
            "new_rows": _refresh["new_rows"],
            "error": _refresh["error"],
            "stage": _refresh["stage"],
            "log": _refresh["log"][-60:],
        }
    snap["elapsed"] = (round(time.time() - snap["started_at"], 1)
                       if snap["running"] and snap["started_at"] else None)
    return snap


def _run_step(script, extra=None, stage=None):
    """跑一个子步骤，stdout 逐行收进刷新日志。返回退出码（异常返回 -1）。

    `script` 是相对 ROOT 的路径片段（如 ["backend", "fetch_data.py"]），
    `extra` 是命令行参数（如 ["--full"]）—— 两者必须分开，
    否则 os.path.join 会把参数当成路径拼上去（2026-10-04 实测踩到）。
    """
    if stage:
        with _REFRESH_LOCK:
            _refresh["stage"] = stage
    cmd = [sys.executable, os.path.join(ROOT, *script)] + list(extra or [])
    env = dict(os.environ)
    env["NO_PROXY"] = env.get("NO_PROXY", "127.0.0.1,localhost")
    env["PYTHONIOENCODING"] = "utf-8"      # 否则 Windows 下中文日志乱码
    try:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            with _REFRESH_LOCK:
                _refresh["log"].append(line)
                if len(_refresh["log"]) > _REFRESH_LOG_MAX:
                    del _refresh["log"][0]
        return proc.wait()
    except Exception as e:
        traceback.print_exc()
        with _REFRESH_LOCK:
            _refresh["log"].append(f"步骤异常：{e}")
        return -1


def _refresh_worker(full=False):
    """后台线程：① 抓股指+铁矿期货日线 ② 重建铁矿石序列 ③ 清进程内缓存。

    必须异步 —— 抓取要几分钟，同步等会直接撞上前端 20 秒超时。
    """
    try:
        rc = _run_step(["backend", "fetch_data.py"], ["--full"] if full else [],
                       stage="股指与期货日线")

        new_rows = None
        with _REFRESH_LOCK:
            for line in _refresh["log"]:
                m = re.search(r"本次新写入期货日线\s*(\d+)\s*行", line)
                if m:
                    new_rows = int(m.group(1))

        # 第二步：重建铁矿石序列（重抓生意社基差率 → 反推现货 → 落盘 json）。
        # 依赖上一步刚入库的期货日线，所以必须放在后面。
        # 失败不回滚、不影响股指 —— 旧的 ironore_series.json 原样保留，页面照常能看，
        # 只是图还停在上一次的数据（前端会看到 error 提示）。
        iron_rc = None
        if rc == 0:
            with _REFRESH_LOCK:
                _refresh["log"].append("———— 重建铁矿石序列（生意社基差率 → 反推现货）————")
            iron_rc = _run_step(["scripts", "build_ironore_series.py"], ["--fetch"],
                                stage="铁矿石序列")

        # 关键一步：清掉进程内缓存，否则页面还是旧数据 —— 等于白跑
        ds.clear_caches()
        engine.clear_caches()
        ironore.clear_cache()

        with _REFRESH_LOCK:
            _refresh["returncode"] = rc
            _refresh["iron_rc"] = iron_rc
            _refresh["new_rows"] = new_rows
            if rc != 0:
                _refresh["error"] = f"抓取脚本退出码 {rc}"
            elif iron_rc != 0:
                _refresh["error"] = "股指数据已更新，但铁矿石重建失败（铁矿石图仍为上一次的数据）"
            else:
                _refresh["error"] = None
    except Exception as e:
        traceback.print_exc()
        with _REFRESH_LOCK:
            _refresh["error"] = str(e)
    finally:
        with _REFRESH_LOCK:
            _refresh["running"] = False
            _refresh["stage"] = None
            _refresh["finished_at"] = time.time()


class Handler(SimpleHTTPRequestHandler):
    # 关键：SimpleHTTPRequestHandler 默认是 HTTP/1.0 —— 每个请求都要新建 TCP 连接、
    # 用完即断。而浏览器（尤其 Edge）按 HTTP/1.1 的 keep-alive 复用连接，
    # 于是出现大量 ConnectionReset / ConnectionAborted，浏览器不断重试，
    # 页面表现为"卡在加载中"。改成 1.1 后连接可复用，加载速度恢复正常。
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self):
        """给 HTML 关掉缓存。

        2026-10-04 真实踩到：新加的「更新数据」按钮死活刷新不出来 ——
        SimpleHTTPRequestHandler 不发 Cache-Control，浏览器就按启发式规则缓存，
        用户看到的还是旧页面。这里显式声明 no-cache，改完前端刷新即可生效。
        """
        if self.path.endswith(".html") or self.path.endswith(".htm"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静音

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                return self.handle_api(path, q)
            # 静态文件
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()
        except Exception as e:
            traceback.print_exc()
            return json_resp(self, {"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        """只有数据更新是 POST；其余一律 404。"""
        u = urlparse(self.path)
        try:
            if u.path == "/api/refresh":
                return self.handle_refresh(parse_qs(u.query))
            return json_resp(self, {"ok": False, "error": "unknown api"}, 404)
        except Exception as e:
            traceback.print_exc()
            return json_resp(self, {"ok": False, "error": str(e)}, 500)

    def handle_refresh(self, q):
        """启动一次后台抓取。已在跑就直接返回当前状态，不重复起线程。"""
        if not ALLOW_REFRESH:
            return json_resp(self, {
                "ok": False,
                "error": "本接口仅在本机开放（线上数据由定时任务自动更新）",
            }, 403)
        with _REFRESH_LOCK:
            already = _refresh["running"]
            if not already:
                _refresh.update({
                    "running": True, "started_at": time.time(), "finished_at": None,
                    "returncode": None, "iron_rc": None, "new_rows": None,
                    "error": None, "stage": None, "log": [],
                })
        if already:
            return json_resp(self, dict(_refresh_snapshot(), already=True))
        full = q.get("full", ["0"])[0] == "1"
        threading.Thread(target=_refresh_worker, args=(full,), daemon=True).start()
        return json_resp(self, {"ok": True, "started": True, "full": full})

    def handle_api(self, path, q):
        def arg(k, default=None):
            v = q.get(k)
            return v[0] if v else default

        if path == "/api/meta":
            return json_resp(self, {
                "ok": True,
                # 只暴露「已端到端接入」的品种（ENABLED_VARIETIES）。
                # 数据层认得但前端没接好的品种（如铁矿石）不出现在这里，
                # 否则前端会多出一个点进去是空表的选项。
                "varieties": {k: {"name": VARIETIES[k]["name"], "mult": VARIETIES[k]["mult"]}
                              for k in ENABLED_VARIETIES},
                "latest_date": ds.get_latest_date(),
                # 前端据此决定要不要显示「更新数据」按钮（线上为 false）
                "allow_refresh": ALLOW_REFRESH,
                # 当前服务进程 PID —— 本地调试用（页面页脚显示，便于确认"改的代码
                # 到底有没有生效 / 是不是起了两个服务"）。前端只在 allow_refresh
                # 为真（= 本机模式）时才显示，线上读者看不到。
                "pid": os.getpid(),
            })

        if path == "/api/refresh_status":
            if not ALLOW_REFRESH:
                return json_resp(self, {"ok": False, "error": "本接口仅在本机开放"}, 403)
            return json_resp(self, dict(_refresh_snapshot(), ok=True))

        if path == "/api/basis":
            variety = arg("variety", "IC")
            rows = ds.compute_basis_table(variety)
            spot = ds.load_spot(variety)
            spot_last = None
            if spot is not None and len(spot):
                spot_last = {"date": spot["date"].iloc[-1], "close": float(spot["spot_close"].iloc[-1])}
            return json_resp(self, {
                "ok": True, "variety": variety,
                "name": VARIETIES.get(variety, {}).get("name", ""),
                "spot": spot_last, "rows": rows,
            })

        # ---- 铁矿石图表（现货 / 最近合约 / 主力合约 / 基差率 / 年度对比）----
        # 数据不是抓来的，是**反推**的：现货 = 主力期货 ÷ (1 − 基差率/100)，
        # 基差率来自生意社 13 年逐日序列，期货来自本库。详见 scripts/build_ironore_series.py。
        if path == "/api/ironore":
            try:
                years = int(arg("years", "3"))
            except ValueError:
                years = 3
            return json_resp(self, ironore.payload(years))

        if path == "/api/realtime":
            variety = arg("variety", "IC")
            return json_resp(self, build_realtime_payload(variety))

        # ---- 批量实时：一次返回四个品种 ----
        # 为什么要这个接口：前端首屏原来并发打 4 个 /api/realtime，加上 /api/meta
        # 共 5 个请求。走 Cloudflare 回源时，只要任意一个请求卡住，
        # Promise.all 就整体挂起 → 页面永远转圈（2026-09-24 线上真实事故）。
        # 新浪接口本身支持逗号批量（见 realtime._fetch_raw），所以能一次性把所有
        # 品种的行情拉回来，四个品种共用一次出网请求。首屏请求数 5 → 2。
        if path == "/api/realtime_all":
            # 只拉「已接入」的品种 —— 数据层认得但没接好的品种（铁矿石）没有
            # 现货代码，会把 None 塞进新浪请求导致 500（2026-10-04 实测踩到）。
            varieties = list(ENABLED_VARIETIES)
            # 一次性收集全部合约代码 + 现货代码，合成单次新浪请求
            all_syms = []
            sym_map = {}
            for v in varieties:
                syms = ds.get_active_symbols(v)
                sym_map[v] = syms
                all_syms.extend(syms)
            all_quotes = rt.fetch_futures_quote(all_syms)

            payloads = {}
            for v in varieties:
                cfg = VARIETIES.get(v, {})
                # 从批量结果里筛出本品种的合约
                mine = [q for q in all_quotes if q["symbol"] in set(sym_map[v])]
                spot_q = rt.fetch_index_quote(cfg.get("spot_code"))
                payloads[v] = build_realtime_payload(v, quotes=mine, spot_q=spot_q)
            return json_resp(self, {
                "ok": True,
                "varieties": payloads,
                "is_trading": rt.is_trading_time(),
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        if path == "/api/history":
            variety = arg("variety", "IC")
            years = int(arg("years", "1"))
            df = engine.variety_history(variety)
            if df is None or not len(df):
                return json_resp(self, {"ok": True, "variety": variety, "series": [], "stats": None})
            import pandas as pd
            if years > 0:
                cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
                sub = df[pd.to_datetime(df["date"]) >= cutoff]
                if len(sub) < 20:
                    sub = df
            else:
                sub = df
            cur = sub.iloc[-1]
            # 当前合约代号（次月合约），用于取实时价
            cur_symbol = cur["symbol"]

            # ---- 实时价覆盖"当前"这一点（跟基差看板/移仓助手口径一致）----
            live_annual = None
            live_basis = None
            live_spot = None
            live_date = None
            is_live = False
            try:
                cfg = VARIETIES.get(variety, {})
                spot_q = rt.fetch_index_quote(cfg.get("spot_code"))
                if spot_q and spot_q.get("price"):
                    qs = rt.fetch_futures_quote([cur_symbol])
                    if qs and qs[0].get("price"):
                        lp = qs[0]["price"]
                        sp = spot_q["price"]
                        ltd = ds.symbol_last_trade_date(cur_symbol)
                        from datetime import datetime as _dt
                        days_left = None
                        if ltd:
                            days_left = (_dt.strptime(ltd, "%Y-%m-%d").date() - _dt.now().date()).days
                        if days_left and days_left > 0:
                            live_spot = sp
                            live_basis = sp - lp
                            live_annual = (live_basis / sp) * 365 / days_left * 100
                            live_date = _dt.now().strftime("%Y-%m-%d")
                            is_live = True
            except Exception:
                pass

            ann_series = sub["annual"].dropna()
            cur_annual = float(cur["annual"]) if pd.notna(cur["annual"]) else None
            cur_basis = float(cur["basis"])

            series = [{"date": r["date"], "annual": r["annual"], "basis": r["basis"],
                       "spot": r["spot"], "symbol": r["symbol"]}
                      for _, r in sub.iterrows()]

            # ---- 三个指标各自算一套统计（前端切换指标时直接用，无需重新请求）----
            # 当前值优先用实时价
            hist_rows = series  # 不含实时点，作为分位基准
            live_cur = {}
            if is_live:
                live_cur = {"annual": live_annual, "basis": live_basis, "spot": live_spot}
                series.append({
                    "date": "今", "annual": round(live_annual, 2),
                    "basis": round(live_basis, 2), "spot": round(live_spot, 2),
                    "symbol": cur_symbol, "live": True,
                })

            metrics = {}
            for mk in ("annual", "basis", "spot"):
                st = engine.metric_stats(hist_rows, mk, current=live_cur.get(mk))
                if st:
                    metrics[mk] = st
            # 兼容旧字段结构（前端已改为读 metrics，这里保留 cur_symbol 等）
            a_stats = metrics.get("annual") or {}
            return json_resp(self, {
                "ok": True, "variety": variety,
                "name": VARIETIES.get(variety, {}).get("name", ""),
                "series": series,
                "is_live": is_live,
                "is_trading": rt.is_trading_time(),
                "server_time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "metrics": metrics,
                "stats": {
                    "cur_annual": a_stats.get("current"),
                    "cur_basis": (metrics.get("basis") or {}).get("current", cur_basis),
                    "cur_symbol": cur_symbol,
                    "percentile": a_stats.get("percentile"),
                    "q25": a_stats.get("q25"),
                    "q50": a_stats.get("q50"),
                    "q75": a_stats.get("q75"),
                    "sample": int(len(sub)),
                    "start": sub["date"].iloc[0], "end": sub["date"].iloc[-1],
                    "live_date": live_date,
                },
            })

        if path == "/api/move":
            variety = arg("variety", "IC")
            years = int(arg("years", "1"))
            # 尝试取实时价，用于计算"当前"价差与分位
            live_prices = None
            live_date = None
            try:
                syms = ds.get_active_symbols(variety)
                qs = rt.fetch_futures_quote(syms)
                lp = {q["symbol"]: q["price"] for q in qs if q.get("price")}
                if lp:
                    live_prices = lp
                    live_date = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            except Exception:
                pass
            rows = engine.move_helper_table(variety, years=years,
                                            live_prices=live_prices, live_date=live_date)
            for r in rows:
                r.pop("series", None)
            return json_resp(self, {
                "ok": True, "variety": variety, "rows": rows,
                "is_trading": rt.is_trading_time(),
                "is_live": bool(live_prices),
                "server_time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        if path == "/api/pair":
            near = arg("near")
            far = arg("far")
            years = int(arg("years", "1"))
            if not near or not far:
                return json_resp(self, {"ok": False, "error": "need near & far"}, 400)
            a = engine.pair_analysis(near, far, years=years)
            return json_resp(self, {"ok": True, "data": a})

        return json_resp(self, {"ok": False, "error": "unknown api"}, 404)


def main():
    port = int(os.environ.get("FUTURE_PORT", DEFAULT_PORT))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"future 服务已启动: http://127.0.0.1:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
