# -*- coding: utf-8 -*-
"""本地 HTTP API：给浏览器插件提供买卖点（只绑 127.0.0.1，不对外）。

    GET /api/health                          -> {ok, service, version}
    GET /api/symbols                         -> {symbols: [{market, code, name}]}
    GET /api/signal?code=HK:00700&tf=1d      -> 算法信号（默认 boll_atr）
        code 支持 HK:00700 / US:AAPL / CRYPTO:BTC/USDT / 00700 / AAPL / BTC/USDT
        tf   1d / 1m / 5m / 15m / 30m / 60m（分钟档取最近 400 根计算）
        algo boll_atr（signals.SIGNAL_ALGOS 注册名）

GUI 启动即起（端口 config.api.port，默认 7132）。
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("market_sniper.api")

_server: ThreadingHTTPServer | None = None
_port: int | None = None


class _Handler(BaseHTTPRequestHandler):

    # ---- 输出 ----
    def _send(self, status: int, payload: dict | None = None):
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def log_message(self, *args):  # 静默访问日志
        pass

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/health":
                from market_sniper import __version__
                return self._send(200, {"ok": True,
                                        "service": "market-sniper",
                                        "version": __version__})

            if u.path == "/api/symbols":
                return self._send(200, {"symbols": db_list_universe()})

            if u.path == "/api/signal":
                return self._handle_signal(q)

            if u.path == "/api/backtest":
                return self._handle_backtest(q)

            return self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("API 处理失败: %s", self.path)
            return self._send(500, {"error": str(e)})

    def _handle_backtest(self, q: dict):
        """标准回测：与 CLI/GUI 共用 compute/ 计算层。"""
        from market_sniper import compute as compute_mod
        from market_sniper.symbols import to_local

        raw = (q.get("code") or [""])[0].strip()
        tf = (q.get("tf") or ["1d"])[0].strip()
        algo = (q.get("algo") or ["boll_atr"])[0].strip()
        try:
            nbars = max(60, min(5000, int((q.get("bars") or ["800"])[0])))
            fee = float((q.get("fee_bps") or ["5"])[0])
            slip = float((q.get("slippage_bps") or ["2"])[0])
            cash = float((q.get("cash") or ["1000000"])[0])
        except ValueError:
            return self._send(400, {"error": "bad numeric param"})
        if not raw:
            return self._send(400, {"error": "code required"})
        try:
            local = to_local(raw)
        except Exception:
            return self._send(400, {"error": f"bad code: {raw}"})
        market, _, code = local.partition(":")

        if tf == "1d":
            bars = db_load_daily(market, code, nbars)
        elif tf in ("1m", "5m", "15m", "30m", "60m"):
            bars = db_load_min(market, code, tf, nbars)
        else:
            return self._send(400, {"error": f"bad tf: {tf}"})
        if not bars or not len(bars["close"]):
            return self._send(404, {"error": f"no local bars: {local} {tf}"})

        try:
            out = compute_mod.backtest(
                algo, bars, market, tf,
                bt_params={"fee_bps": fee, "slippage_bps": slip,
                           "initial_cash": cash},
                )
        except Exception as e:
            return self._send(500, {"error": f"backtest failed: {e}"})
        out.update({"code": local, "tf": tf, "market": market})
        return self._send(200, out)

    def _handle_signal(self, q: dict):
        from market_sniper import signals as sig_mod
        from market_sniper.symbols import to_local

        raw = (q.get("code") or [""])[0].strip()
        tf = (q.get("tf") or ["1d"])[0].strip()
        algo_name = (q.get("algo") or ["boll_atr"])[0].strip()
        try:
            limit = max(1, min(1000, int((q.get("limit") or ["200"])[0])))
        except ValueError:
            limit = 200
        if not raw:
            return self._send(400, {"error": "code required"})

        try:
            local = to_local(raw)
        except Exception:
            return self._send(400, {"error": f"bad code: {raw}"})
        market, _, code = local.partition(":")

        fn = sig_mod.SIGNAL_ALGOS.get(algo_name)
        if fn is None:
            return self._send(400, {
                "error": f"unknown algo: {algo_name}",
                "algos": sorted(sig_mod.SIGNAL_ALGOS),
            })

        if tf == "1d":
            bars = db_load_daily(market, code, 400)
        elif tf in ("1m", "5m", "15m", "30m", "60m"):
            bars = db_load_min(market, code, tf, 400)
        else:
            return self._send(400, {"error": f"bad tf: {tf}"})
        if not bars or not len(bars["close"]):
            return self._send(404, {"error": f"no local bars: {local} {tf}，"
                                             "先在程序里回填"})

        out = fn(bars, max_signals=limit, market=market)
        out.update({"code": local, "tf": tf, "market": market})
        return self._send(200, out)


# ---- db 薄封装（避免模块级 import cycle，按需调用） ----
def db_list_universe() -> list[dict]:
    from market_sniper.data import db
    return [{"market": r["market"], "code": r["code"], "name": r.get("name") or ""}
            for r in db.list_universe()]


def db_load_daily(market: str, code: str, limit: int):
    from market_sniper.data import db
    return db.load_daily_bars(market, code, limit=limit)


def db_load_min(market: str, code: str, tf: str, limit: int):
    from market_sniper.data import db
    return db.load_min_bars(market, code, tf, limit=limit)


# ---- 生命周期 ----
def start(port: int | None = None) -> int | None:
    """启动 API 服务（已启动则忽略）。返回端口；绑定失败返回 None。"""
    global _server, _port
    if _server is not None:
        return _port
    from market_sniper import config as config_mod
    port = int(port or config_mod.get_config().get("api.port", 7132))
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        log.warning("API 端口 %s 绑定失败（可能已开着另一个实例）: %s", port, e)
        _server = None
        return None
    _port = port
    threading.Thread(target=_server.serve_forever,
                     name="market-sniper-api", daemon=True).start()
    log.info("API 已启动 http://127.0.0.1:%s", port)
    return port


def stop() -> None:
    global _server, _port
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
        _port = None
        log.info("API 已停止")


def running_port() -> int | None:
    return _port


if __name__ == "__main__":
    import time

    logging.basicConfig(level=logging.INFO)
    p = start()
    print(f"serving on http://127.0.0.1:{p}  (Ctrl+C 退出)")
    while True:
        time.sleep(3600)
