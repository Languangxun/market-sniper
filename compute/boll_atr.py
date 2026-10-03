#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""boll_atr.py —— 计算端信号脚本（自包含）。

stdin:  {"market": "US", "tf": "1d",
         "bars": {"dates"/"ts": [...], "open": [...], "high": [...],
                  "low": [...], "close": [...], "volume": [...]},
         "params": {"n": 20, "k": 2.0, "atr_n": 14, "max_signals": 200}}
stdout: {"algo": "boll_atr", "signals": [...], "last_close": float,
         "bars": int, "position": "多头/空头/空仓", "params": {...}}

信号 = 仓位动作序列（状态机）：
    US/CRYPTO: open_long / close_long / open_short / close_short
    HK(只多):  buy / sell
"""
import json
import sys

import numpy as np


def ts_to_ms(s):
    import datetime
    if not s:
        return None
    s = str(s)
    try:
        if len(s) == 10:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d")
        else:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def sma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.convolve(x, np.ones(n) / n, mode="valid")
    return out


def roll_std(x, n):
    out = np.full(len(x), np.nan)
    cs = np.cumsum(np.concatenate(([0.0], x * x)))
    sm = np.cumsum(np.concatenate(([0.0], x)))
    for i in range(n - 1, len(x)):
        s = sm[i + 1] - sm[i + 1 - n]
        q = cs[i + 1] - cs[i + 1 - n]
        out[i] = max(0.0, q / n - (s / n) ** 2) ** 0.5
    return out


def atr(h, l, c, n):
    pc = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return sma(tr, n)


def compute(payload):
    p = payload.get("params") or {}
    n = int(p.get("n", 20))
    k = float(p.get("k", 2.0))
    atr_n = int(p.get("atr_n", 14))
    max_signals = int(p.get("max_signals", 200))

    bars = payload["bars"]
    c = np.asarray(bars["close"], dtype=float)
    h = np.asarray(bars["high"], dtype=float)
    l = np.asarray(bars["low"], dtype=float)
    dates = bars.get("dates") or bars.get("ts") or []
    market = (payload.get("market") or "US").upper()

    upper = sma(c, n) + k * roll_std(c, n)
    lower = sma(c, n) - k * roll_std(c, n)
    atrv = atr(h, l, c, atr_n)
    allow_short = market in ("US", "CRYPTO")

    signals = []
    pos = "flat"

    def emit(i, side):
        ts = str(dates[i]) if i < len(dates) else ""
        a = float(atrv[i])
        sig = {"ts": ts, "ts_ms": ts_to_ms(ts) or 0, "side": side,
               "price": round(float(c[i]), 6), "atr": round(a, 6)}
        if side in ("open_long", "buy"):
            sig["stop"] = round(float(c[i] - k * a), 6)
        elif side == "open_short":
            sig["stop"] = round(float(c[i] + k * a), 6)
        signals.append(sig)

    for i in range(max(1, n), len(c)):
        if np.isnan(upper[i]) or np.isnan(atrv[i]):
            continue
        up_reg = c[i - 1] < lower[i - 1] and c[i] > lower[i]
        dn_reg = c[i - 1] > upper[i - 1] and c[i] < upper[i]
        if up_reg:
            if pos == "flat":
                emit(i, "open_long" if allow_short else "buy")
                pos = "long"
            elif pos == "short":
                emit(i, "close_short")
                pos = "flat"
        if dn_reg:
            if pos == "flat" and allow_short:
                emit(i, "open_short")
                pos = "short"
            elif pos == "long":
                emit(i, "close_long" if allow_short else "sell")
                pos = "flat"

    return {"algo": "boll_atr",
            "params": {"n": n, "k": k, "atr_n": atr_n,
                       "allow_short": allow_short},
            "signals": signals[-max_signals:],
            "last_close": round(float(c[-1]), 6) if len(c) else None,
            "bars": len(c),
            "position": {"flat": "空仓", "long": "多头", "short": "空头"}[pos]}


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{}")
    json.dump(compute(payload), sys.stdout, ensure_ascii=False)
