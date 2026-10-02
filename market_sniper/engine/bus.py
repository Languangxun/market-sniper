# -*- coding: utf-8 -*-
"""线程安全事件总线：feed 线程 emit，GUI/策略订阅。"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable

log = logging.getLogger("market_sniper.engine.bus")


class EventBus:
    def __init__(self):
        self._subs: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, topic: str, fn: Callable) -> None:
        with self._lock:
            if fn not in self._subs[topic]:
                self._subs[topic].append(fn)

    def off(self, topic: str, fn: Callable) -> None:
        with self._lock:
            if fn in self._subs.get(topic, []):
                self._subs[topic].remove(fn)

    def emit(self, topic: str, payload) -> None:
        with self._lock:
            handlers = list(self._subs.get(topic, ()))
        for fn in handlers:
            try:
                fn(payload)
            except Exception:
                log.exception("bus handler failed: topic=%s fn=%s", topic, fn)

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()
