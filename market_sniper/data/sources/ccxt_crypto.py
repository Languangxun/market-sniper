# -*- coding: utf-8 -*-
"""ccxt 加密数据源（Binance 主，OKX 备）。

统一接口：
    fetch_ohlcv(symbol, timeframe='1d', since_ms=None, limit=1000)
        -> list[dict(ts, open, high, low, close, volume)]
    fetch_markets(quote='USDT')
        -> list[dict(symbol, base, quote, active)]
"""
from __future__ import annotations

import logging
import threading
import time

import ccxt

log = logging.getLogger("market_sniper.sources.ccxt")

_ex_cache: dict[str, ccxt.Exchange] = {}
_ex_lock = threading.Lock()


def _proxies() -> dict:
    from market_sniper import config as config_mod
    url = config_mod.get_config().proxy_url()
    return {"http": url, "https": url} if url else {}


def reset_exchanges() -> None:
    with _ex_lock:
        _ex_cache.clear()


def _make_ex(name: str) -> ccxt.Exchange:
    cls = getattr(ccxt, name)
    cfg = {
        "enableRateLimit": True,
        "proxies": _proxies(),
        "timeout": 15000,
        "options": {"adjustForTimeDifference": True},
    }
    return cls(cfg)


def get_ex(name: str = "binance") -> ccxt.Exchange:
    if name not in _ex_cache:
        with _ex_lock:
            if name not in _ex_cache:
                try:
                    _ex_cache[name] = _make_ex(name)
                except Exception as e:
                    log.error("init exchange %s failed: %s", name, e)
                    raise
    return _ex_cache[name]


# 优先顺序基础池：config.sources.CRYPTO 置顶，其余兜底
_BASE_ORDER = ("binance", "okx", "bybit", "gate")


def exchange_order() -> list[str]:
    from market_sniper import config as config_mod
    pref = str(config_mod.get_config().get("sources.CRYPTO", "binance")
               or "binance").lower()
    if pref in _BASE_ORDER:
        return [pref] + [x for x in _BASE_ORDER if x != pref]
    return list(_BASE_ORDER)


def _try_exchanges():
    for name in exchange_order():
        try:
            ex = get_ex(name)
            yield name, ex
        except Exception as e:
            log.warning("exchange %s unavailable: %s", name, e)
            continue


# -------- 单标 K 线（带多交易所容灾）--------
def fetch_ohlcv(symbol: str, *, timeframe: str = "1d",
                since_ms: int | None = None, limit: int = 1000) -> list[dict]:
    """symbol = 'BTC/USDT'，timeframe = '1m'/'5m'/'15m'/'30m'/'1h'/'1d'/'1w'。

    返回 [{ts(ms), date('YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS+00:00'),
           open, high, low, close, volume}, ...]
    """
    last = None
    for name, ex in _try_exchanges():
        try:
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe,
                                 since=since_ms, limit=limit)
            if not raw:
                continue
            return _normalize(raw, timeframe, source=name)
        except Exception as e:
            last = e
            log.warning("%s %s/%s 拉取失败: %s", name, symbol, timeframe, e)
            continue
    raise RuntimeError(f"all exchanges failed for {symbol} {timeframe}: {last}")


def fetch_ohlcv_range(symbol: str, *, timeframe: str = "1d",
                      since_ms: int, until_ms: int | None = None,
                      batch: int = 1000) -> list[dict]:
    """分页拉取（穿越多批），返回 [start, end] 全量K线。"""
    out: list[dict] = []
    cursor = since_ms
    while True:
        rows = fetch_ohlcv(symbol, timeframe=timeframe,
                           since_ms=cursor, limit=batch)
        if not rows:
            break
        out.extend(rows)
        cursor = rows[-1]["ts_ms"] + 1
        if until_ms and cursor >= until_ms:
            break
        if len(rows) < batch:
            break
    return out


# -------- 标的全集 --------
def fetch_markets(quote: str = "USDT", exchange: str = "binance") -> list[dict]:
    """返回 [{symbol, base, quote, active}, ...]；用于择日内时间。"""
    ex = get_ex(exchange)
    markets = ex.load_markets()
    out = []
    for sym, m in markets.items():
        if not m.get("active"):
            continue
        if m.get("quote") != quote:
            continue
        if m.get("type") not in ("spot",):
            continue
        out.append({
            "symbol": sym, "base": m["base"], "quote": quote,
            "exchange": exchange, "active": True,
        })
    return out


def normalize_symbol(s: str) -> str:
    """'CRYPTO:BTC/USDT' 或 'BTC/USDT' -> 'BTC/USDT'。"""
    if ":" in s:
        s = s.split(":", 1)[1]
    return s.upper()


# -------- helpers --------
def _normalize(raw: list[list], timeframe: str, source: str) -> list[dict]:
    out = []
    for ts_ms, o, h, l, c, v in raw:
        ts_ms = int(ts_ms)
        if timeframe.endswith("d") or timeframe.endswith("w"):
            date = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
        else:
            date = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                 time.gmtime(ts_ms / 1000))
        out.append({
            "ts": date, "ts_ms": ts_ms, "date": date,
            "open": float(o), "high": float(h),
            "low": float(l), "close": float(c),
            "volume": float(v), "source": source,
        })
    return out


def dt_to_ms(d: str) -> int:
    """'YYYY-MM-DD' -> UTC ms。"""
    import datetime
    dt = datetime.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


if __name__ == "__main__":
    rows = fetch_ohlcv("BTC/USDT", timeframe="1d", limit=5)
    for r in rows:
        print(r)
    print("---")
    rows = fetch_ohlcv("BTC/USDT", timeframe="1m", limit=3)
    for r in rows:
        print(r)