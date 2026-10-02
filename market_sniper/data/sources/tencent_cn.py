# -*- coding: utf-8 -*-
"""CN/A 股数据源：yfinance 主（覆盖深沪主板+创业板+科创板）。

支持：
    sh600519 / sz000001 / sz300750 / sh688981 等
    sh/sz 都通过 yfinance 走（SH 形如 '600519.SS'，SZ 形如 '000001.SZ'）。
不支持：
    bj920002 等北交所（Yahoo 不支持）。

复权：yfinance 默认 auto_adjust=False，返回的 close 是**未复权**价。
调用方需要自行保存并处理分红送配（TODO v0.2 接新浪复权因子）。
"""
from __future__ import annotations

import logging

from market_sniper.data.sources import yfinance_us as _yf

log = logging.getLogger("market_sniper.sources.tencent_cn")


def _to_yf(code: str) -> str:
    c = code.lower()
    if c.startswith("sh") and len(c) == 8 and c[2:].isdigit():
        return f"{c[2:]}.SS"
    if c.startswith("sz") and len(c) == 8 and c[2:].isdigit():
        return f"{c[2:]}.SZ"
    if c.startswith("bj"):
        raise ValueError(f"yfinance 不支持北交所: {code}")
    raise ValueError(f"not a CN code: {code}")


def fetch_daily(code: str, *, period: str = "max",
               start: str | None = None, end: str | None = None) -> list[dict]:
    return _yf.fetch_daily(_to_yf(code), period=period, start=start, end=end)


def fetch_minute(code: str, *, interval: str = "1m",
                 period: str = "7d") -> list[dict]:
    return _yf.fetch_minute(_to_yf(code), interval=interval, period=period)


def fetch_quote(code: str) -> dict | None:
    return _yf.fetch_quote(_to_yf(code))


if __name__ == "__main__":
    for c in ("sh600519", "sz000001", "sz300750", "sh688981"):
        rows = fetch_daily(c, period="5d")
        print(c, "rows:", len(rows), "last:", rows[-1]["close"] if rows else None)