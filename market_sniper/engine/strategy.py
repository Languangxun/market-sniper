# -*- coding: utf-8 -*-
"""策略基类：算法实现 on_tick / on_bar，返回 Signal 或 None。

示例：
    class MyStrategy(Strategy):
        name = "my"

        def on_tick(self, tick):
            if self.ready(tick):
                return Signal(tick.market, tick.code, tick.ts_ms,
                              "buy", tick.price, strategy=self.name)
"""
from __future__ import annotations

from .types import Bar, Signal, Tick


class Strategy:
    name = "strategy"

    def __init__(self, params: dict | None = None):
        self.params = dict(params or {})

    def on_start(self, ctx=None) -> None:
        """引擎启动时回调（ctx 为 LiveEngine）。"""

    def on_tick(self, tick: Tick) -> Signal | None:
        return None

    def on_bar(self, bar: Bar) -> Signal | None:
        return None

    def on_stop(self) -> None:
        """引擎停止时回调。"""
