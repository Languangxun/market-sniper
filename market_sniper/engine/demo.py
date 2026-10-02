# -*- coding: utf-8 -*-
"""演示策略：仅用于 MockFeed 时验证「feed → 策略 → 信号 → GUI」全链路。"""
from __future__ import annotations

from .strategy import Strategy
from .types import Signal, Tick


class DemoStrategy(Strategy):
    """每 N 个 tick 交替发一次 buy/sell（模拟买卖点）。"""

    name = "demo"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.every = max(2, int(self.params.get("every", 40)))
        self._n = 0
        self._side = "buy"

    def on_tick(self, tick: Tick) -> Signal | None:
        self._n += 1
        if self._n % self.every:
            return None
        sig = Signal(
            market=tick.market, code=tick.code, ts_ms=tick.ts_ms,
            side=self._side, price=tick.price,
            strength=1.0, reason=f"demo#{self._n}", strategy=self.name,
        )
        self._side = "sell" if self._side == "buy" else "buy"
        return sig
