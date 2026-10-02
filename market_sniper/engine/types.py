# -*- coding: utf-8 -*-
"""实时引擎事件类型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tick:
    market: str
    code: str
    ts_ms: int
    price: float
    qty: float = 0.0
    side: str = ""              # 'buy' / 'sell' / ''
    source: str = ""

    @property
    def local(self) -> str:
        return f"{self.market}:{self.code}"


@dataclass(frozen=True)
class Quote:
    market: str
    code: str
    ts_ms: int
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    source: str = ""

    @property
    def local(self) -> str:
        return f"{self.market}:{self.code}"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else 0.0


@dataclass
class Bar:
    market: str
    code: str
    timeframe: str
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = False
    source: str = ""

    @property
    def local(self) -> str:
        return f"{self.market}:{self.code}"


@dataclass
class Signal:
    market: str
    code: str
    ts_ms: int
    side: str                    # 'buy' / 'sell'
    price: float
    strength: float = 1.0
    reason: str = ""
    strategy: str = ""

    @property
    def local(self) -> str:
        return f"{self.market}:{self.code}"

    def as_dict(self) -> dict:
        return {
            "market": self.market, "code": self.code, "local": self.local,
            "ts_ms": self.ts_ms, "side": self.side, "price": self.price,
            "strength": self.strength, "reason": self.reason,
            "strategy": self.strategy,
        }
