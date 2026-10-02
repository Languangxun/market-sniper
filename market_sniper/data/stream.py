# -*- coding: utf-8 -*-
"""实时行情流：Feed 协议 + MockFeed（离线模拟）+ BinanceFeed（WebSocket）。

约定：
    Feed.start(emit) 非阻塞（实现内部自带后台线程）；
    emit 在 feed 线程被调用，消费端（LiveEngine）要保证轻量。

Binance 组合流：
    wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@kline_1s/...
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Callable, Protocol

from market_sniper.engine.types import Bar, Quote, Tick

log = logging.getLogger("market_sniper.data.stream")


class Feed(Protocol):
    def start(self, emit: Callable) -> None: ...
    def stop(self) -> None: ...


def _wire_symbol(symbol: str) -> str:
    """'BTC/USDT' -> 'btcusdt'（Binance stream 名）。"""
    return symbol.replace("/", "").replace("-", "").lower()


def _base_link(symbol: str) -> str:
    """'BTC/USDT' -> 'btc'（Quote source 标记用）。"""
    return symbol.split("/", 1)[0].lower()


# ---------------- 离线模拟 ----------------
class MockFeed:
    """随机游走模拟源：框架自测/无网环境用。"""

    def __init__(self, symbols: list[str], *, market: str = "CRYPTO",
                 interval: float = 0.05, seed: int = 7):
        self.symbols = list(symbols)
        self.market = market
        self.interval = max(0.01, float(interval))
        self.seed = seed
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._emit: Callable | None = None

    def start(self, emit: Callable) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._emit = emit
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mock-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        rng = random.Random(self.seed)
        px = {s: 100.0 * (i + 1) for i, s in enumerate(self.symbols)}
        bucket: dict[str, dict] = {}
        n = 0
        while not self._stop.is_set():
            n += 1
            now_ms = int(time.time() * 1000)
            for s in self.symbols:
                price = px[s] * (1 + rng.uniform(-0.0015, 0.0015))
                px[s] = price
                qty = round(rng.uniform(0.001, 2.0), 4)
                self._emit(Tick(self.market, s, now_ms, round(price, 4), qty,
                                side=rng.choice(("buy", "sell")), source="mock"))
                b = bucket.get(s)
                if b is None or now_ms // 1000 != b["sec"]:
                    if b is not None:
                        self._emit(Bar(self.market, s, "1s", b["sec"] * 1000,
                                       b["o"], b["h"], b["l"], b["c"],
                                       volume=b["v"], closed=True, source="mock"))
                    b = bucket[s] = {"sec": now_ms // 1000,
                                     "o": price, "h": price, "l": price,
                                     "c": price, "v": qty}
                else:
                    b["h"] = max(b["h"], price)
                    b["l"] = min(b["l"], price)
                    b["c"] = price
                    b["v"] += qty
            time.sleep(self.interval)


# ---------------- Binance WebSocket ----------------
class BinanceFeed:
    def __init__(self, symbols: list[str], *, market: str = "CRYPTO",
                 proxy: str | None = None,
                 streams: dict | None = None,
                 reconnect_max_sec: int = 30,
                 ws_base: str = "wss://stream.binance.com:9443/stream?streams="):
        self.symbols = list(symbols)
        self.market = market
        self.proxy = proxy
        self.streams = dict(streams or {})
        self.reconnect_max_sec = max(5, int(reconnect_max_sec))
        self.ws_base = ws_base
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._emit: Callable | None = None
        self._ws = None
        self._symbol_map = {_wire_symbol(s): s for s in self.symbols}

    # ---- URL ----
    def _stream_names(self) -> list[str]:
        names = []
        for s in self.symbols:
            w = _wire_symbol(s)
            if self.streams.get("agg_trade", True):
                names.append(f"{w}@aggTrade")
            if self.streams.get("kline_1s", True):
                names.append(f"{w}@kline_1s")
            if self.streams.get("book_ticker", True):
                names.append(f"{w}@bookTicker")
        return names

    def url(self) -> str:
        return self.ws_base + "/".join(self._stream_names())

    # ---- 生命周期 ----
    def start(self, emit: Callable) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._emit = emit
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="binance-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    # ---- 内部 ----
    def _run(self) -> None:
        from websockets.sync.client import connect

        backoff = 1.0
        while not self._stop.is_set():
            try:
                log.info("连接 Binance WS（%d streams，proxy=%s）",
                         len(self._stream_names()), self.proxy)
                with connect(self.url(), proxy=self.proxy, open_timeout=15,
                             ping_interval=20, ping_timeout=20,
                             close_timeout=5) as ws:
                    self._ws = ws
                    backoff = 1.0
                    for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle(raw)
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("Binance WS 断开: %s（%.0fs 后重连）", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.reconnect_max_sec)
            finally:
                self._ws = None

    def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        data = msg.get("data") if isinstance(msg, dict) else None
        if data is None:
            data = msg
        etype = data.get("e")
        try:
            if etype == "aggTrade":
                self._on_agg_trade(data)
            elif etype == "kline":
                self._on_kline(data)
            elif "bookTicker" in str(msg.get("stream", "")) or \
                    ("b" in data and "a" in data and "u" in data):
                self._on_book_ticker(data)
        except Exception:
            log.exception("解析 Binance 消息失败: %s", str(msg)[:200])

    def _code(self, wire: str) -> str:
        return self._symbol_map.get(wire.lower(), wire.upper())

    def _on_agg_trade(self, d: dict) -> None:
        self._emit(Tick(
            market=self.market,
            code=self._code(d.get("s", "")),
            ts_ms=int(d.get("T") or d.get("E") or 0),
            price=float(d.get("p") or 0.0),
            qty=float(d.get("q") or 0.0),
            side="sell" if d.get("m") else "buy",
            source="binance",
        ))

    def _on_kline(self, d: dict) -> None:
        k = d.get("k") or {}
        self._emit(Bar(
            market=self.market,
            code=self._code(k.get("s", "")),
            timeframe="1s",
            ts_ms=int(k.get("t") or 0),
            open=float(k.get("o") or 0.0),
            high=float(k.get("h") or 0.0),
            low=float(k.get("l") or 0.0),
            close=float(k.get("c") or 0.0),
            volume=float(k.get("v") or 0.0),
            closed=bool(k.get("x")),
            source="binance",
        ))

    def _on_book_ticker(self, d: dict) -> None:
        self._emit(Quote(
            market=self.market,
            code=self._code(d.get("s", "")),
            ts_ms=int(d.get("E") or time.time() * 1000),
            bid=float(d.get("b") or 0.0),
            ask=float(d.get("a") or 0.0),
            bid_qty=float(d.get("B") or 0.0),
            ask_qty=float(d.get("A") or 0.0),
            source="binance",
        ))


# ---------------- 工厂 ----------------
def create_feed(config) -> Feed:
    backend = str(config.get("feed.backend", "binance")).lower()
    symbols = config.feed_symbols()
    market = config.get("feed.market", "CRYPTO")
    if not symbols:
        raise ValueError("feed.symbols 为空，请在设置里配置标的")
    if backend == "mock":
        return MockFeed(symbols, market=market)
    if backend == "binance":
        return BinanceFeed(
            symbols, market=market,
            proxy=config.proxy_url(),
            streams=config.get("feed.streams") or {},
            reconnect_max_sec=int(config.get("feed.reconnect_max_sec", 30) or 30),
        )
    raise ValueError(f"未知 feed.backend: {backend}")
