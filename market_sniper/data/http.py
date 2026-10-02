# -*- coding: utf-8 -*-
"""HTTP 客户端：多源熔断 + 指数退避 + 代理路由（借鉴 stock_predict 思路精简）。

熔断逻辑：
    每个 src_name 维护 (连续失败数, 熔断截止时间戳)。
    失败且属于 503/限流类 → 失败数+1，达到阈值后按指数延长冷却；
    成功 → 清零，立即结束熔断。

代理路由：
    国际域名默认走代理；探测到代理被拒（端口关闭）后短旁路，
    避免每次请求都双倍超时。

线程安全：模块全局字典 + Lock；并发调用安全。
"""
from __future__ import annotations

import logging
import random
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("market_sniper.http")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# -------- 代理配置（运行时由 settings/env 注入） --------
_proxy_opener: object | None = None
_proxy_lock = threading.Lock()

# 国际数据源域名（ccxt/yfinance/AAStocks）走代理；国内 cn 后缀直连
_INTL_SUFFIX = (
    "binance.com", "okx.com", "bybit.com", "coinbase.com", "kraken.com",
    "yahoo.com", "yfinance", "aastocks.com", "hsi.com.hk", "gov.hk",
    "ft.com", "wsj.com",
)


def set_proxy(opener):
    """注入一个 urllib opener；None 清除。"""
    global _proxy_opener
    with _proxy_lock:
        _proxy_opener = opener


def build_proxy_opener(proxy_url: str | None):
    """根据形如 http://127.0.0.1:7890 的字符串构造 opener。"""
    if not proxy_url:
        return None
    try:
        proxy = urllib.request.ProxyHandler({
            "http": proxy_url, "https": proxy_url,
        })
        opener = urllib.request.build_opener(proxy)
        opener.addheaders = [("User-Agent", UA)]
        return opener
    except Exception as e:                                 # pragma: no cover
        log.warning("代理构造失败: %s", e)
        return None


# -------- 熔断器 --------
_SOURCE_CB: dict[str, list[float]] = {}   # name -> [fail_streak, cb_until]
_cb_lock = threading.Lock()
CB_THRESHOLD = 2
CB_BASE_COOLDOWN = 60.0
CB_MAX_COOLDOWN = 600.0

# 代理连接失败后短暂旁路
_PROXY_DEAD_UNTIL = [0.0]


def _cb_ok(name: str) -> bool:
    st = _SOURCE_CB.get(name)
    return not (st and st[1] > 0 and time.time() < st[1])


def _cb_record(name: str, ok: bool, err: Exception | None = None) -> None:
    ratelimited = (not ok) and _is_ratelimit_err(err) if err is not None else False
    with _cb_lock:
        st = _SOURCE_CB.setdefault(name, [0.0, 0.0])
        if ok:
            st[0], st[1] = 0.0, 0.0
            return
        st[0] += 1
        if ratelimited and st[0] >= CB_THRESHOLD:
            cd = min(CB_BASE_COOLDOWN * (2 ** (st[0] - CB_THRESHOLD)),
                     CB_MAX_COOLDOWN)
            st[1] = max(st[1], time.time() + cd)


def _is_ratelimit_err(e: Exception | None) -> bool:
    if not e:
        return False
    code = getattr(e, "code", None)
    if code is not None and code in (429, 501, 502, 503, 504):
        return True
    s = str(e)
    return any(t in s for t in ("RemoteDisconnected", "Connection reset",
                                "Connection aborted", "Too Many Requests",
                                "RateLimitExceeded"))


def cb_state() -> dict[str, tuple[float, float]]:
    """返回熔断快照（debug / GUI 状态栏用）。"""
    now = time.time()
    return {n: (v[0], max(0.0, v[1] - now)) for n, v in _SOURCE_CB.items()}


# -------- 退避 --------
def backoff(attempt: int, base: float = 0.5, cap: float = 6.0) -> float:
    d = min(base * (2 ** attempt), cap)
    return d * (0.75 + random.random() * 0.5)


# -------- 主入口 --------
def _is_intl_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s) for s in _INTL_SUFFIX)


def _proxy_dead() -> bool:
    return time.time() < _PROXY_DEAD_UNTIL[0]


def _mark_proxy_dead(seconds: float = 120.0) -> None:
    _PROXY_DEAD_UNTIL[0] = time.time() + seconds


def _open(req, url: str, timeout: float):
    """按域名选 channel；代理被拒自动旁路。返回 bytes；都失败抛最后一个异常。"""
    can_proxy = _proxy_opener is not None and not _proxy_dead()
    if not can_proxy:
        order = [None]
    elif _is_intl_url(url):
        order = [_proxy_opener, None]
    else:
        order = [None, _proxy_opener]
    last: Exception | None = None
    for opener in order:
        try:
            if opener is None:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if last is None:
                last = e
            if opener is not None and "refused" in str(e).lower():
                _mark_proxy_dead()
            continue
    raise last if last is not None else RuntimeError("no usable channel")


def http_get(url: str, *, retries: int = 3, timeout: float = 12.0,
            headers: dict | None = None, src_name: str | None = None,
            decode: str | None = "utf-8") -> bytes | str:
    """HTTP GET；限流类错误只做1次重试，普通错误做全量重试。

    返回 bytes（decode=None）或 str（按 decode 解码）。
    src_name 非空时上报熔断。
    """
    hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdr.update(headers)
    last: Exception | None = None
    ok = False
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            raw = _open(req, url, timeout)
            ok = True
            return raw if decode is None else raw.decode(decode, errors="ignore")
        except Exception as e:
            last = e
            eff = min(retries, 2) if _is_ratelimit_err(e) else retries
            if attempt + 1 >= eff:
                break
            time.sleep(backoff(attempt))
    raise RuntimeError(f"http_get failed: {last}") from last if not ok else None


def http_post(url: str, data: dict | bytes | str, *, retries: int = 3,
              timeout: float = 12.0, headers: dict | None = None,
              src_name: str | None = None, decode: str | None = "utf-8"):
    """HTTP POST；data 为 dict 时自动 form-encode。"""
    hdr = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdr.update(headers)
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        hdr.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, str):
        body = data.encode()
    else:
        body = data
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
            raw = _open(req, url, timeout)
            return raw if decode is None else raw.decode(decode, errors="ignore")
        except Exception as e:
            last = e
            eff = min(retries, 2) if _is_ratelimit_err(e) else retries
            if attempt + 1 >= eff:
                break
            time.sleep(backoff(attempt))
    raise RuntimeError(f"http_post failed: {last}") from last