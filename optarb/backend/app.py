# -*- coding: utf-8 -*-
"""
Optarb HTTP 服务

关键性能约定（沿用 future / ETFarb 踩过的坑）：
  1. protocol_version = "HTTP/1.1"  否则 keep-alive 冲突 → 页面卡死
  2. 端口 8104（避开 8000/8102/8103）
"""
import json
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, entry, expiry_window, live, market_calendar, migrate, quotes, risk, store  # noqa: E402
from datetime import date  # noqa: E402

FRONTEND = os.path.join(ROOT, "frontend")


def jd(obj):
    """容错 JSON：NaN/Infinity -> null（否则 json.dumps 会产出非法 JSON 导致前端解析失败）"""
    return json.dumps(_nan_safe(obj), ensure_ascii=False, default=str)


def _nan_safe(o):
    if isinstance(o, float):
        if o != o or o in (float("inf"), float("-inf")):
            return None
        return o
    if isinstance(o, dict):
        return {str(k): _nan_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_nan_safe(v) for v in o]
    return o


# ------------------------------------------------------------------ 数据更新
# ★ 2026-09-28：东哥要「页面上有个更新按钮」，不要额外的 bat 文件。
#
# 为什么要后台线程 + 前端轮询，不能同步返回：
#   更新要抓中金所 10 天窗口的日行情（单日 600+ 合约）+ 3 条指数日线，
#   实测 5~30s。前端 `get()` 超时 20s，**同步等一定会被误杀**
#   （future 项目的教训：超时值要实测，不能拍脑袋）。
#   所以：点一下 → 立即返回「已开始」→ 前端每 1.5s 轮询状态 → 完成自动刷新。
_UPDATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "success": None,      # None=没跑过 / True / False
    "output": "",
    "error": None,
    "days": 10,
}
_UPDATE_LOCK = threading.Lock()


def _run_update(days=10):
    """后台跑 scripts/update_market.py。

    用**子进程**而不是 import：抓取脚本崩溃/卡死时不会连累 web 服务，
    而且它本来就是独立可跑的（东哥手工也能跑）。
    `sys.executable` = 当前服务的 Python，避免解释器不一致。
    """
    import subprocess
    env = dict(os.environ)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "update_market.py"),
             "--days", str(days)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
        )
        out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
        with _UPDATE_LOCK:
            _UPDATE["success"] = (p.returncode == 0)
            _UPDATE["output"] = out[-3000:]
            _UPDATE["error"] = None if p.returncode == 0 else (
                err[-1500:] or f"exit code {p.returncode}")
    except Exception as e:  # noqa: BLE001
        with _UPDATE_LOCK:
            _UPDATE["success"] = False
            _UPDATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _UPDATE_LOCK:
            _UPDATE["running"] = False
            _UPDATE["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _start_update(days=10):
    with _UPDATE_LOCK:
        if _UPDATE["running"]:
            return {"ok": True, "running": True, "msg": "已经在更新了，请稍候"}
        _UPDATE.update(running=True, success=None, output="", error=None,
                       days=days, finished_at=None,
                       started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    threading.Thread(target=_run_update, args=(days,), daemon=True).start()
    return {"ok": True, "running": True, "msg": "已开始更新"}


def _update_status():
    with _UPDATE_LOCK:
        return dict(_UPDATE, ok=True)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # ★ 绝不能删（见 future 项目的坑）

    def log_message(self, fmt, *args):
        if os.environ.get("OPTARB_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -------------------------------------------------- 静态
    # ★ 2026-09-30：本地文件名 = 线上路由名，并且**本地也支持线上那三个短地址**。
    #   这样 http://127.0.0.1:8104/option-daily 与
    #        https://<域名>/option-daily   是同一个 URL，本地看到什么线上就是什么。
    #   （原来本地文件叫 index/migrate/expiry.html，导航链接 option-*.html 全是 404。）
    ROUTES = {
        "/": "option.html",              # 本地入口（线上 / 是门户页，不是 Optarb）
        "/option": "option.html",
        "/option.html": "option.html",
        "/option-daily": "option-daily.html",
        "/option-daily.html": "option-daily.html",
        "/option-risk": "option-risk.html",
        "/option-risk.html": "option-risk.html",
        "/option-entry": "option-entry.html",
        "/option-entry.html": "option-entry.html",
    }

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path.startswith("/api/"):
                return self._api(path)
            if path in self.ROUTES:
                return self._send_file(os.path.join(FRONTEND, self.ROUTES[path]),
                                       "text/html")
            if path == "/favicon.ico":
                return self._send(b"", "image/x-icon")
            return self._send_file(os.path.join(FRONTEND, path.lstrip("/")),
                                   self._guess_type(path))
        except Exception as e:
            traceback.print_exc()
            return self._send_json({"ok": False, "error": str(e)}, 500)

    def _guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {".html": "text/html", ".js": "application/javascript",
                ".css": "text/css", ".json": "application/json",
                ".png": "image/png", ".svg": "image/svg+xml"}.get(ext, "text/plain")

    # -------------------------------------------------- 工具
    def _send(self, body: bytes, ctype="text/plain; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(jd(obj).encode("utf-8"), "application/json; charset=utf-8", code)

    def _send_file(self, p, ctype):
        if not os.path.exists(p):
            return self._send_json({"ok": False, "error": f"not found: {p}"}, 404)
        with open(p, "rb") as f:
            data = f.read()
        self._send(data, f"{ctype}; charset=utf-8")

    def _query(self):
        from urllib.parse import unquote
        q = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = unquote(v)
        return q

    # -------------------------------------------------- API
    def _api(self, path):
        q = self._query()

        if path == "/api/meta":
            return self._send_json({
                "ok": True,
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "varieties": config.VARIETIES,
                "ladder": config.LADDER,
                "scenarios": config.SCENARIOS,
                "multiplier": config.MULTIPLIER,
                # ★ 2026-10-03：市场状态。页面据此提示「休市中，显示最近收盘」，
                #   也让「现在还要不要抓实时」这件事对用户透明。
                "market": market_calendar.status(),
            })

        if path == "/api/chain":
            variety = (q.get("variety") or "IO").upper()
            month = q.get("month") or None
            ch = quotes.fetch_option_chain(variety, month)
            # chain 的 tuple key 不能直接 JSON，转成可序列化
            chain_list = []
            for (m, cp, k), o in sorted(ch["chain"].items()):
                chain_list.append(o)
            return self._send_json({
                "ok": True,
                "variety": ch["variety"],
                "spot": ch["spot"],
                "spot_samples": ch["spot_samples"],
                "months": ch["months"],
                "strikes": ch["strikes"],
                "count": ch["count"],
                "options": chain_list,
            })

        if path == "/api/futures":
            variety = (q.get("variety") or "IF").upper()
            return self._send_json({"ok": True, "rows": quotes.fetch_futures(variety)})

        if path == "/api/analyze":
            return self._analyze(q)

        # ---------------- 到期日 + 剩余自然日（前端"剩余自然日"自动填用） ----------------
        if path == "/api/expiry":
            v = (q.get("variety") or "MO").upper()
            m = q.get("month") or ""
            if not m:
                return self._send_json({"ok": False, "error": "month 必填"})
            conn = store.connect()
            try:
                exp = migrate.effective_expiry(conn, v, m)
                dte = (date.fromisoformat(exp) - date.today()).days
                return self._send_json({"ok": True, "expiry": exp, "dte": dte})
            finally:
                conn.close()

        # ---------------- 移仓价差（复刻参考作者的日报） ----------------
        if path == "/api/migrate/dates":
            conn = store.connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT date FROM opt_daily ORDER BY date DESC LIMIT 90").fetchall()
                return self._send_json({"ok": True, "dates": [r["date"] for r in rows]})
            finally:
                conn.close()

        if path == "/api/migrate":
            return self._migrate(q)

        # ---------------- 交割日窗口（历史分布） ----------------
        if path == "/api/window":
            conn = store.connect()
            try:
                return self._send_json(expiry_window.build_window_cached(
                    conn,
                    (q.get("variety") or "MO").upper(),
                    cp=(q.get("cp") or "P").upper(),
                    months=int(q.get("months") or 12),
                    offset=int(q.get("offset") or 0),
                    tmax=int(q.get("tmax") or 10)))
            finally:
                conn.close()

        # ---------------- 实时移仓价差（盘中决策） ----------------
        if path == "/api/live":
            conn = store.connect()
            try:
                out = live.live_spread(
                    conn,
                    (q.get("variety") or "MO").upper(),
                    cp=(q.get("cp") or "P").upper())
                if isinstance(out, dict):
                    out["market"] = market_calendar.status()
                return self._send_json(out)
            finally:
                conn.close()

        # ---------------- 数据更新（页面上点按钮，不用 bat） ----------------
        if path == "/api/update":
            return self._send_json(_start_update(int(q.get("days") or 10)))
        if path == "/api/update/status":
            return self._send_json(_update_status())

        # ---------------- 入场判断（IV 序列 + 六格指标 + 一句话结论） ----------------
        #   口径与措辞定稿见 docs/009 §4；实现见 core/entry.py
        if path == "/api/iv":
            return self._iv(q)
        if path == "/api/entry":
            return self._entry(q)

        return self._send_json({"ok": False, "error": f"unknown api: {path}"}, 404)

    def _iv(self, q):
        """IV 序列 + 分位。underlying 必填；window = 只取最近 N 个交易日。"""
        und = (q.get("underlying") or "510300").upper()
        window = q.get("window")
        conn = store.connect()
        try:
            from core import etf_iv
            series = etf_iv.load_series(conn, und)
            if not series:
                return self._send_json({
                    "ok": False, "underlying": und,
                    "error": "该标的还没有 IV 数据。先跑：python scripts/fetch_etf_iv.py --backfill"})
            if window:
                series = series[-int(window):]
            pcts = {}
            for label, w in (("all", None), ("y1", 243), ("y3", 243 * 3)):
                p = etf_iv.percentile(conn, und, window=w)
                pcts[label] = p
            return self._send_json({
                "ok": True, "underlying": und,
                "name": etf_iv.UNDERLYING_NAME.get(und, und),
                "series": [{"date": d, "iv": v} for d, v in series],
                "latest": etf_iv.latest(conn, und),
                "percentiles": pcts,
                "stats": etf_iv.stats(conn),
                # ★ 页面必须把这段白话显示在 IV 那一格旁边（东哥 2026-10-01 要求）
                "caliber_note": etf_iv.CALIBER_NOTE,
            })
        finally:
            conn.close()

    def _entry(self, q):
        """六格指标 + 四段式一句话结论。"""
        und = (q.get("underlying") or "510300").upper()
        conn = store.connect()
        try:
            nofetch = (q.get("nofetch") or "") == "1"
            out = entry.entry_report(conn, und, fetch_index=not nofetch)
            # ★ 2026-10-03：把市场状态一起下发，前端才能标「休市 · 显示最近收盘」。
            #   这样做是为了**不额外增加一个 /api/meta 请求**（首屏请求越少越稳）。
            if isinstance(out, dict):
                out["market"] = market_calendar.status()
            return self._send_json(out)
        finally:
            conn.close()

    def _migrate(self, q):
        variety = (q.get("variety") or "IO").upper()
        cp = (q.get("cp") or "P").upper()
        conn = store.connect()
        try:
            date_iso = q.get("date") or store.latest_date(conn, variety)
            if not date_iso:
                return self._send_json({
                    "ok": False,
                    "error": "库里还没有数据。先跑：python scripts/update_market.py --days 10"})
            rep = migrate.build_table(conn, variety, date_iso, cp=cp,
                                      near=q.get("near") or None, far=q.get("far") or None)
            if rep.get("ok"):
                rep["expiries"] = store.load_expiries(conn, variety, date_iso)
                rep["futures"] = store.load_futures(conn, config.FUTURES_MAP.get(variety, ""), date_iso)
                rep["available_dates"] = [
                    r["date"] for r in conn.execute(
                        "SELECT DISTINCT date FROM opt_daily WHERE variety=? "
                        "ORDER BY date DESC LIMIT 90", (variety,)).fetchall()]
            return self._send_json(rep)
        finally:
            conn.close()

    def _analyze(self, q):
        variety = (q.get("variety") or "IO").upper()
        month = q.get("month") or None
        dte = int(q.get("dte") or 30)

        legs = []
        # legs 参数：cp,strike,qty,premium,month,variety 用 ; 分隔多腿
        # ★ 2026-09-29：每条腿可带自己的品种码（各腿品种独立，不跟「① 行情」联动）
        raw = q.get("legs") or ""
        for part in raw.split(";"):
            part = part.strip()
            if not part:
                continue
            f = part.split(",")
            if len(f) < 3:
                continue
            legs.append({
                "cp": f[0].upper(),
                "strike": float(f[1]),
                "qty": int(float(f[2])),
                "premium": float(f[3]) if len(f) > 3 and f[3] else 0.0,
                "month": f[4] if len(f) > 4 and f[4] else None,
                "variety": (f[5].upper() if len(f) > 5 and f[5] else None),
            })

        if not legs:
            return self._send_json({"ok": False, "error": "没有持仓"})

        # 行情链按「第一条腿的品种」取（同品种持仓时就是它本身）
        primary = legs[0].get("variety") or variety
        ch = quotes.fetch_option_chain(primary, month)
        for l in legs:
            if not l.get("variety"):
                l["variety"] = primary
            if not l.get("month"):
                l["month"] = month or (ch["months"][0] if ch["months"] else "")

        # ★ 混品种持仓：各品种都要自己的链（保证金必须用**该腿自己品种**的现货/IV/乘数）
        chains = {primary: ch}
        for lv in {l["variety"] for l in legs}:
            if lv not in chains:
                try:
                    chains[lv] = quotes.fetch_option_chain(lv, month)
                except Exception:
                    pass

        rep = risk.analyze(ch, legs, dte, variety=primary, chains=chains)
        # 补充每条腿的即时信息（行情链只覆盖主品种的合约，别的品种腿取不到就是 None）
        iv_map = risk.build_iv_map(ch, ch["spot"], dte)
        for l in rep["legs"]:
            if l.get("variety") != primary:
                continue
            o = ch["chain"].get((l["month"], l["cp"], l["strike"]))
            if o:
                l["instrument"] = o["instrument"]
                l["bid"] = o.get("bid")
                l["ask"] = o.get("ask")
                l["last"] = o.get("last")
                l["iv"] = iv_map.get((l["cp"], l["strike"]))

        rep["ok"] = True
        rep["variety"] = primary
        return self._send_json(rep)


def warmup():
    """
    启动时后台预热：把默认品种的期权链抓一次填进缓存。

    为什么要做：服务进程「第一次」对外发 HTTP 请求时，会有一笔一次性开销
    （实测可达 15 秒；第二次起只要 0.005 秒）。如果让页面的首个请求去承担
    这笔开销，用户会看到 15 秒白屏。放在启动时预热，用户打开页面就是瞬间的。

    ★ 2026-10-03（东哥要求）：**休市时直接跳过**。
      周末 / 假期 / 盘前 / 收盘后，中金所那边根本没有实时行情，
      启动时去抓纯属浪费 —— 而且缓存 TTL 那时会设成「到下次开市」，
      抓到的就是一份不会再变的数据，等真有访客来时抓一次即可。
      判断逻辑在 `core/market_calendar.py`。
    """
    st = market_calendar.status()
    if not st["is_live"]:
        print(f"[warmup] 跳过：现在{st['label']}，没有实时行情（下次开市 {st['next_session']}）",
              flush=True)
        return

    def _go():
        try:
            t0 = time.time()
            # ★ 2026-09-29：预热 MO（东哥的主战场，页面默认品种）+ IO。
            #   MO 放第一个 —— 用户打开页面立刻要用的是它。
            for v in ("MO", "IO"):
                try:
                    ch = quotes.fetch_option_chain(v)
                    print(f"[warmup] {v}: spot={ch['spot']} count={ch['count']} "
                          f"({time.time() - t0:.2f}s)", flush=True)
                except Exception as e:
                    # 单品种失败不影响另一个，也不拖垮服务
                    print(f"[warmup] {v} 失败（跳过）: {e}", flush=True)
        except Exception as e:
            print(f"[warmup] 失败（不影响服务）: {e}", flush=True)

    threading.Thread(target=_go, daemon=True).start()


def main():
    port = config.PORT
    # ★ 2026-10-01：启动时确保表结构存在（幂等，全是 CREATE TABLE IF NOT EXISTS）。
    #   踩过：推了新代码但库里还没有 etf_iv_daily（该表原先只在 update_market.py /
    #   fetch_etf_iv.py 里建），服务显示 active 但 /api/entry 直接 500
    #   「no such table: etf_iv_daily」——"服务 active ≠ 数据正常"。
    try:
        store.init_db()
        print("  表结构已就绪", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 初始化表结构失败（服务继续启动）: {e}", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Optarb 已启动: http://127.0.0.1:{port}", flush=True)
    print(f"  数据源: 中金所 quote_*.txt", flush=True)
    warmup()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
