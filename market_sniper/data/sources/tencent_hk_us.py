# -*- coding: utf-8 -*-
"""腾讯行情实时接口（HK/US 个股 + 指数）。

qt.gtimg.cn 一次请求可批量多个标的，单标的约 0.1~0.4s，无延迟；
只负责实时报价，日K/分钟K 仍走 yfinance。

代码映射：
    HK:00700 -> hk00700          US:AAPL  -> usAAPL
    US:^DJI  -> usDJI            US:^GSPC -> usINX
    US:^IXIC -> usIXIC           US:^HSI  -> hkHSI（恒指存 US 通道）

输出与 yfinance_us.fetch_quote 同构：
    {last, prev_close, open, day_high, day_low, year_high, year_low,
     volume, amount, currency, name, ts, source}
"""
from __future__ import annotations

import logging
import re
import threading

import requests

from market_sniper import config as config_mod

log = logging.getLogger("market_sniper.sources.tencent")

_BASE = "https://qt.gtimg.cn/q="

_INDEX_MAP = {
    "^DJI": "usDJI",
    "^GSPC": "usINX",
    "^IXIC": "usIXIC",
    "^HSI": "hkHSI",
}

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
                    "Referer": "https://gu.qq.com/",
                })
                _session = s
    return _session


def to_tencent(market: str, code: str) -> str:
    up = code.strip().upper()
    if up.startswith("^"):
        return _INDEX_MAP.get(up, "us" + up[1:])
    if market.upper() == "HK":
        return "hk" + code.strip().zfill(5)
    return "us" + up.replace("-", ".")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse(payload: str, market: str) -> dict | None:
    p = payload.split("~")
    if len(p) < 35:
        return None
    last = _num(p[3])
    if not last:
        return None
    name = (p[46].strip() if len(p) > 46 and p[46].strip() else p[1].strip())
    currency = (p[75].strip() if len(p) > 75 and p[75].strip()
                else ("HKD" if market.upper() == "HK" else "USD"))
    return {
        "last": last,
        "prev_close": _num(p[4]),
        "open": _num(p[5]),
        "day_high": _num(p[33]) if len(p) > 33 else None,
        "day_low": _num(p[34]) if len(p) > 34 else None,
        "year_high": _num(p[48]) if len(p) > 48 else None,
        "year_low": _num(p[49]) if len(p) > 49 else None,
        "volume": _num(p[6]),
        "amount": _num(p[37]) if len(p) > 37 else None,
        "currency": currency,
        "name": name,
        "ts": p[30].strip() if len(p) > 30 else None,
        "source": "tencent",
    }


def fetch_quotes(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """pairs = [('HK', '00700'), ('US', 'AAPL'), ...]

    返回 {local: quote}，local = 'MARKET:CODE'。
    """
    if not pairs:
        return {}
    codes: dict[str, str] = {}
    for market, code in pairs:
        codes[f"{market.upper()}:{code}"] = to_tencent(market, code)
    url = _BASE + ",".join(codes.values())
    try:
        r = _get_session().get(url, timeout=10)
        r.raise_for_status()
        r.encoding = "gbk"
    except Exception as e:
        log.debug("tencent fetch failed: %s", e)
        return {}
    by_tcode = {v.lower(): k for k, v in codes.items()}
    out: dict[str, dict] = {}
    for tcode, payload in re.findall(r'v_([^=]+)="([^"]*)"', r.text):
        local = by_tcode.get(tcode.lower())
        if not local:
            continue
        q = _parse(payload, local.split(":", 1)[0])
        if q:
            out[local] = q
    return out


def fetch_quote(market: str, code: str) -> dict | None:
    return fetch_quotes([(market, code)]).get(f"{market.upper()}:{code}")


if __name__ == "__main__":
    import time

    for pair in (("HK", "00700"), ("US", "AAPL"), ("US", "^DJI"),
                 ("US", "^GSPC"), ("US", "^IXIC"), ("US", "^HSI")):
        t0 = time.time()
        q = fetch_quote(*pair)
        print(pair, f"{time.time()-t0:.2f}s",
              {k: q.get(k) for k in ("last", "prev_close", "name")} if q else None)
