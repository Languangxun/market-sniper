# -*- coding: utf-8 -*-
"""yfinance 数据源：US / HK / CN 共用。

直接走 Yahoo Finance v8 chart endpoint（curl 形态），不依赖 yfinance 的
Ticker.info / quoteSummary（对部分 HK 标的会 404）。这层只负责：
    daily    :  period='max' / '5y' / '1y' ...
    1m       :  period='7d'  / '30d' / '60d' / '1y'
    5m/15m   :  period='60d'
    1h       :  period='730d'

输出统一为 {ts(ms), date('YYYY-MM-DD' 或 'YYYY-MM-DDTHH:MM:SS+00:00'),
           open, high, low, close, volume}。
"""
from __future__ import annotations

import datetime
import logging
import threading
import time

import requests

from market_sniper import config as config_mod

log = logging.getLogger("market_sniper.sources.yf")

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

_session: requests.Session | None = None
_lock = threading.Lock()


def _proxies() -> dict:
    url = config_mod.get_config().proxy_url()
    return {"http": url, "https": url} if url else {}


def reset_session() -> None:
    global _session
    with _lock:
        _session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                s = requests.Session()
                s.proxies = _proxies()
                s.headers.update({
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/126.0 Safari/537.36"),
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                _session = s
    return _session


# Yahoo interval 允许的取值
INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m",
             "1h", "1d", "5d", "1wk", "1mo", "3mo"}


def _fetch_chart(yf_symbol: str, *, interval: str, period: str | None = None,
                 range_from: int | None = None, range_to: int | None = None
                 ) -> dict:
    """直接调 chart endpoint。

    period / range_from-to 二选一。
    """
    if interval not in INTERVALS:
        raise ValueError(f"bad interval: {interval}")
    params = {"interval": interval, "includePrePost": "false",
              "events": "div,split"}
    if period:
        params["range"] = period
    elif range_from and range_to:
        params["period1"] = int(range_from)
        params["period2"] = int(range_to)
    else:
        params["range"] = "1mo"
    url = f"{_BASE}/{yf_symbol}"
    s = _get_session()
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = s.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            j = j = r.json()
            err = (j.get("chart") or {}).get("error")
            if err:
                raise RuntimeError(f"yahoo error: {err}")
            return j
        except Exception as e:
            last = e
            time.sleep(0.5 + attempt * 0.5)
    raise RuntimeError(f"yahoo fetch failed: {last}")


def _chart_to_rows(j: dict, interval: str) -> list[dict]:
    """解析 Yahoo chart 响应。"""
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return []
    res0 = res[0]
    ts_list = res0.get("timestamp") or []
    ind = (res0.get("indicators") or {}).get("quote") or [{}]
    ohlc0 = ind[0] if ind else {}
    opens = ohlc0.get("open") or []
    highs = ohlc0.get("high") or []
    lows = ohlc0.get("low") or []
    closes = ohlc0.get("close") or []
    vols = ohlc0.get("volume") or []

    out: list[dict] = []
    for i, ts in enumerate(ts_list):
        o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], vols[i]
        if o is None or c is None:
            continue
        ts_ms = int(ts) * 1000
        if interval in ("1d", "5d", "1wk", "1mo", "3mo"):
            date = time.strftime("%Y-%m-%d", time.gmtime(ts))
        else:
            date = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                  time.gmtime(ts))
        out.append({
            "ts": date, "ts_ms": ts_ms, "date": date,
            "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "volume": float(v or 0),
            "source": "yahoo",
        })
    return out


# ---------------- 主入口 ----------------
def fetch_daily(yf_symbol: str, *, period: str = "max",
               start: str | None = None, end: str | None = None) -> list[dict]:
    """日K。period in {'max','5y','10y','1y','6mo','1mo'} 等。"""
    if start or end:
        p1 = int(datetime.datetime.strptime(start, "%Y-%m-%d").timestamp()) if start else 0
        p2 = int(datetime.datetime.strptime(end, "%Y-%m-%d").timestamp()) + 86399 if end else int(time.time())
        j = _fetch_chart(yf_symbol, interval="1d",
                         range_from=p1, range_to=p2)
    else:
        j = _fetch_chart(yf_symbol, interval="1d", period=period)
    return _chart_to_rows(j, "1d")


def fetch_minute(yf_symbol: str, *, interval: str = "1m",
                 period: str = "7d") -> list[dict]:
    """分钟K。interval in {1m,5m,15m,30m,60m,1h}；最长 7d(1m)/60d/730d(1h)。"""
    if interval not in INTERVALS or interval in ("1d", "5d", "1wk", "1mo", "3mo"):
        raise ValueError(f"not a minute interval: {interval}")
    j = _fetch_chart(yf_symbol, interval=interval, period=period)
    return _chart_to_rows(j, interval)


def fetch_quote(yf_symbol: str) -> dict | None:
    """通过 1d chart 拿 meta（不再调 quoteSummary）。"""
    try:
        j = _fetch_chart(yf_symbol, interval="1d", period="5d")
    except Exception as e:
        log.debug("fetch_quote failed for %s: %s", yf_symbol, e)
        return None
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return None
    meta = res[0].get("meta") or {}
    rows = _chart_to_rows(j, "1d")
    if not rows:
        return None
    last = rows[-1]["close"]
    prev = float(meta.get("chartPreviousClose") or 0) or None
    return {
        "last": float(last),
        "prev_close": prev,
        "open": float(meta.get("regularMarketOpen") or rows[-1]["open"]) or None,
        "day_high": float(meta.get("regularMarketDayHigh") or rows[-1]["high"]) or None,
        "day_low": float(meta.get("regularMarketDayLow") or rows[-1]["low"]) or None,
        "year_high": meta.get("fiftyTwoWeekHigh"),
        "year_low": meta.get("fiftyTwoWeekLow"),
        "volume": rows[-1]["volume"],
        "currency": meta.get("currency"),
        "name": meta.get("longName") or meta.get("shortName"),
    }


if __name__ == "__main__":
    rows = fetch_daily("AAPL", period="5d")
    print("AAPL day last:", rows[-1] if rows else None)
    rows = fetch_minute("AAPL", interval="1m", period="5d")
    print("AAPL 1m last:", rows[-1] if rows else None)
    print("quote:", fetch_quote("AAPL"))
    rows = fetch_daily("0700.HK", period="5d")
    print("0700.HK day last:", rows[-1] if rows else None)
    rows = fetch_minute("0700.HK", interval="1m", period="5d")
    print("0700.HK 1m last:", rows[-1] if rows else None)