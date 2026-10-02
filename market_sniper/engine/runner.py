# -*- coding: utf-8 -*-
"""实时引擎：feed → 环形缓冲 → 策略 → 信号总线（+ 可选 tick 落盘）。

线程模型：
    feed 自带后台线程，通过 emit 回调进入 _on_event；
    _on_event 在 feed 线程内跑策略；策略必须轻量、不可阻塞。
    PyQt 侧订阅 bus 时用信号桥回主线程（pyqtSignal 线程安全）。

用法：
    eng = LiveEngine(config, strategies=[MyStrategy()])
    eng.bus.on("signal", on_signal)
    eng.start()
    ...
    eng.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from market_sniper import config as config_mod
from market_sniper.data import db

from .bus import EventBus
from .strategy import Strategy
from .types import Bar, Quote, Signal, Tick

log = logging.getLogger("market_sniper.engine")


class _TickWriter(threading.Thread):
    """批量落盘：feed 线程 append，后台线程定期 flush，避免每 tick 写 SQLite。"""

    def __init__(self, flush_ms: int = 1000):
        super().__init__(name="tick-writer", daemon=True)
        self.flush_ms = max(50, int(flush_ms))
        self.flushed = 0
        self._pending: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def add(self, market: str, code: str, row: dict) -> None:
        with self._lock:
            self._pending.append((market, code, row))

    def flush(self) -> int:
        with self._lock:
            if not self._pending:
                return 0
            batch, self._pending = self._pending, []
        groups: dict[tuple[str, str], list[dict]] = {}
        for market, code, row in batch:
            groups.setdefault((market, code), []).append(row)
        n = 0
        for (market, code), rows in groups.items():
            try:
                n += db.insert_ticks(market, code, rows, source="stream")
            except Exception:
                log.exception("tick 落盘失败 %s:%s (%d 条)", market, code, len(rows))
        self.flushed += n
        return n

    def run(self) -> None:
        while not self._stop.wait(self.flush_ms / 1000.0):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=3)
        self.flush()


class LiveEngine:
    def __init__(self, config: config_mod.Config | None = None,
                 feed=None, strategies: list[Strategy] | None = None):
        self.config = config or config_mod.get_config()
        self.bus = EventBus()
        self.strategies: list[Strategy] = list(strategies or [])
        self.feed = feed
        self.ring_size = max(100, int(self.config.get("feed.ring_size", 5000) or 5000))

        self._ticks: dict[str, deque] = {}
        self._bars: dict[str, deque] = {}
        self._quotes: dict[str, Quote] = {}
        self._last_tick: dict[str, Tick] = {}
        self._lock = threading.Lock()
        self._running = False
        self._started_at: float | None = None
        self._counters = {"tick": 0, "bar": 0, "quote": 0, "signal": 0}

        self._persist = bool(self.config.get("feed.persist_ticks", False))
        self._flush_ms = int(self.config.get("feed.flush_ms", 1000) or 1000)
        self._writer: _TickWriter | None = None
        self._strategy_errors: dict[str, int] = {}

    # -------- 生命周期 --------
    def start(self) -> None:
        if self._running:
            return
        if self.feed is None:
            from market_sniper.data.stream import create_feed
            self.feed = create_feed(self.config)
        if self._persist:
            self._writer = _TickWriter(self._flush_ms)
            self._writer.start()
        for s in self.strategies:
            try:
                s.on_start(self)
            except Exception:
                log.exception("strategy on_start 失败: %s", s.name)
        self._running = True
        self._started_at = time.time()
        try:
            self.feed.start(self._on_event)
        except Exception:
            self._running = False
            if self._writer is not None:
                self._writer.stop()
                self._writer = None
            raise
        self.bus.emit("engine", {"event": "started",
                                 "feed": type(self.feed).__name__})
        log.info("实时引擎启动: feed=%s symbols=%s",
                 type(self.feed).__name__, self.config.feed_symbols())

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self.feed is not None:
                self.feed.stop()
        except Exception:
            log.exception("feed stop 异常")
        for s in self.strategies:
            try:
                s.on_stop()
            except Exception:
                log.exception("strategy on_stop 失败: %s", s.name)
        if self._writer is not None:
            self._writer.stop()
            self._writer = None
        self.bus.emit("engine", {"event": "stopped"})
        log.info("实时引擎停止")

    def add_strategy(self, strategy: Strategy) -> None:
        self.strategies.append(strategy)
        if self._running:
            try:
                strategy.on_start(self)
            except Exception:
                log.exception("strategy on_start 失败: %s", strategy.name)

    # -------- feed 回调（feed 线程） --------
    def _on_event(self, ev) -> None:
        if not self._running:
            return
        try:
            if isinstance(ev, Tick):
                self._on_tick(ev)
            elif isinstance(ev, Bar):
                self._on_bar(ev)
            elif isinstance(ev, Quote):
                self._on_quote(ev)
        except Exception:
            log.exception("引擎处理事件失败: %r", ev)

    def _on_tick(self, tick: Tick) -> None:
        with self._lock:
            self._ticks.setdefault(tick.local, deque(maxlen=self.ring_size)).append(tick)
            self._last_tick[tick.local] = tick
            self._counters["tick"] += 1
        if self._writer is not None:
            self._writer.add(tick.market, tick.code, {
                "ts_ms": tick.ts_ms, "price": tick.price,
                "qty": tick.qty, "side": tick.side,
            })
        self.bus.emit("tick", tick)
        for s in self.strategies:
            self._run_strategy(s.on_tick, tick)

    def _on_bar(self, bar: Bar) -> None:
        with self._lock:
            self._bars.setdefault(bar.local, deque(maxlen=self.ring_size)).append(bar)
            self._counters["bar"] += 1
        self.bus.emit("bar", bar)
        for s in self.strategies:
            self._run_strategy(s.on_bar, bar)

    def _on_quote(self, quote: Quote) -> None:
        with self._lock:
            self._quotes[quote.local] = quote
            self._counters["quote"] += 1
        self.bus.emit("quote", quote)

    def _run_strategy(self, fn, ev) -> None:
        try:
            sig = fn(ev)
        except Exception:
            st = getattr(fn, "__self__", None)
            key = getattr(st, "name", repr(st))
            n = self._strategy_errors.get(key, 0)
            self._strategy_errors[key] = n + 1
            if n == 0:
                log.exception("策略 %s 处理事件失败（后续同类错误降级为 debug）", key)
            else:
                log.debug("策略 %s 处理事件失败（第 %d 次）", key, n + 1)
            return
        if sig is not None:
            self._publish(sig)

    def _publish(self, sig: Signal) -> None:
        with self._lock:
            self._counters["signal"] += 1
        self.bus.emit("signal", sig)

    # -------- 查询 --------
    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict:
        with self._lock:
            last_ts = max((t.ts_ms for t in self._last_tick.values()), default=0)
            return {
                "running": self._running,
                "feed": type(self.feed).__name__ if self.feed else "-",
                "started_at": self._started_at,
                "ticks": self._counters["tick"],
                "bars": self._counters["bar"],
                "quotes": self._counters["quote"],
                "signals": self._counters["signal"],
                "flushed": self._writer.flushed if self._writer else 0,
                "last_ts_ms": last_ts,
                "symbols": sorted(self._ticks.keys()),
            }

    def recent_ticks(self, local: str, limit: int = 100) -> list[Tick]:
        with self._lock:
            buf = self._ticks.get(local)
            return list(buf)[-limit:] if buf else []

    def last_tick(self, local: str) -> Tick | None:
        with self._lock:
            return self._last_tick.get(local)

    def last_quote(self, local: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(local)
