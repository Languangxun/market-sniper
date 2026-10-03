#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest.py —— 标准回测引擎（计算端，自包含）。

模型约定：
    · 信号在**下一根 K 线开盘价**成交（避免未来函数），开盘价加滑点；
    · 手续费按名义金额双边收取（fee_bps，万分之一为单位）；
    · 全仓进出（position_pct 控制仓位比例）；
    · HK 只做多；US/CRYPTO 可做多做空；
    · 逐bar 逐市场净值（equity），支持做空时的负债计价。

stdin:  {"market": "US", "tf": "1d", "bars": {...},
         "signals": [{"ts_ms":..., "side":..., "price":...}, ...],
         "params": {"fee_bps": 5, "slippage_bps": 2, "initial_cash": 1000000,
                    "position_pct": 1.0, "bars_per_year": null}}
stdout: {"metrics": {...}, "trades": [...], "equity": [...], "params": {...}}
"""
import json
import math
import sys


def bars_per_year(market: str, tf: str) -> float:
    if market == "CRYPTO":
        minutes = 365 * 1440
    else:
        session = 390 if market == "US" else 330   # 港股 5.5h
        minutes = 252 * session
    mult = {"1d": 1.0, "60m": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1}
    tf_min = mult.get(tf, 1.0)
    if tf == "1d":
        return minutes / (24 * 60) if market == "CRYPTO" else 252.0
    return minutes / tf_min


def to_ms(ts, i, dates, fallback_step_ms):
    try:
        return int(ts)
    except (TypeError, ValueError):
        pass
    import datetime
    s = str(ts)
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


def run_backtest(payload: dict) -> dict:
    p = payload.get("params") or {}
    fee = float(p.get("fee_bps", 5)) / 10000.0
    slip = float(p.get("slippage_bps", 2)) / 10000.0
    cash0 = float(p.get("initial_cash", 1_000_000))
    pct = min(1.0, max(0.01, float(p.get("position_pct", 1.0))))
    market = (payload.get("market") or "US").upper()
    tf = payload.get("tf") or "1d"
    allow_short = market in ("US", "CRYPTO")
    bpy = float(p.get("bars_per_year") or bars_per_year(market, tf))

    bars = payload["bars"]
    o = [float(x) for x in bars["open"]]
    c = [float(x) for x in bars["close"]]
    dates = bars.get("dates") or bars.get("ts") or []
    nb = len(c)
    step_ms = {"1d": 86400000, "60m": 3600000, "30m": 1800000,
               "15m": 900000, "5m": 300000, "1m": 60000}.get(tf, 60000)

    # 信号 → 执行 bar 索引（下一根开盘），同 bar 只执行第一个动作
    actions = {}
    for sig in payload.get("signals") or []:
        t = to_ms(sig.get("ts_ms") or sig.get("ts"), 0, dates, step_ms)
        if t is None:
            continue
        idx = None
        for i in range(nb):
            ti = to_ms(dates[i], i, dates, step_ms)
            if ti is not None and ti >= t:
                idx = i
                break
        if idx is None or idx + 1 >= nb:
            continue
        actions.setdefault(idx + 1, sig["side"])

    cash = cash0
    pos_qty = 0.0            # >0 多头，<0 空头（股数/币数）
    pos = "flat"
    entry = None             # (ts, price, qty)
    trades = []
    equity_pts = []
    in_market_bars = 0

    def close_trade(i, price):
        nonlocal cash, pos_qty, pos, entry
        ts, e_price, qty = entry
        exit_px = price * (1 - slip) if pos_qty > 0 else price * (1 + slip)
        notional = qty * exit_px
        cash += notional if pos_qty > 0 else -notional
        fee_amt = notional * fee
        cash -= fee_amt
        pnl = (exit_px - e_price) * qty if pos_qty > 0 else (e_price - exit_px) * qty
        trades.append({
            "side": "long" if pos_qty > 0 else "short",
            "entry_ts": ts, "entry_price": round(e_price, 6),
            "exit_ts": str(dates[i]), "exit_price": round(exit_px, 6),
            "qty": round(qty, 8), "pnl": round(pnl, 2),
            "return_pct": round((exit_px / e_price - 1) * 100 *
                                (1 if pos_qty > 0 else -1), 4),
            "hold_bars": i - _entry_i,
        })
        pos_qty = 0.0
        pos = "flat"
        entry = None

    _entry_i = 0

    def open_trade(i, side, price):
        nonlocal cash, pos_qty, pos, entry, _entry_i
        px = price * (1 + slip) if side in ("open_long", "buy") else price * (1 - slip)
        equity_now = cash
        qty = equity_now * pct / px
        if qty <= 0:
            return
        notional = qty * px
        fee_amt = notional * fee
        if side in ("open_long", "buy"):
            cash -= notional + fee_amt
            pos_qty = qty
        else:
            cash += notional - fee_amt     # 空头先收本金
            pos_qty = -qty
        pos = "long" if pos_qty > 0 else "short"
        entry = (str(dates[i]), px, qty)
        _entry_i = i

    for i in range(nb):
        act = actions.get(i)
        if act:
            if act in ("open_long", "buy") and pos == "flat":
                open_trade(i, act, o[i])
            elif act == "open_short" and pos == "flat" and allow_short:
                open_trade(i, act, o[i])
            elif act in ("close_long", "sell") and pos == "long":
                close_trade(i, o[i])
            elif act == "close_short" and pos == "short":
                close_trade(i, o[i])
        # 逐bar净值
        if pos_qty > 0:
            eq = cash + pos_qty * c[i]
        elif pos_qty < 0:
            eq = cash - (-pos_qty) * c[i]
        else:
            eq = cash
        if pos != "flat":
            in_market_bars += 1
        equity_pts.append((dates[i] if i < len(dates) else str(i), eq))

    if entry is not None:                      # 收尾强平
        close_trade(nb - 1, c[nb - 1])

    eq = [e for _, e in equity_pts]
    final = eq[-1] if eq else cash0
    total_ret = (final / cash0 - 1) * 100
    rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
    if final > 0 and eq:
        ann = ((final / cash0) ** (bpy / max(1, len(eq))) - 1) * 100
    else:
        ann = -100.0
    sharpe = 0.0
    if len(rets) > 2:
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        if sd > 0:
            sharpe = mu / sd * math.sqrt(bpy)
    peak, mdd = -1e18, 0.0
    for e in eq:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, e / peak - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in loss))

    # 净值曲线降采样（≤600 点）
    stride = max(1, len(equity_pts) // 600)
    equity_out = [{"ts": t, "equity": round(e, 2)}
                  for t, e in equity_pts[::stride]]

    metrics = {
        "initial_cash": cash0,
        "final_equity": round(final, 2),
        "total_return_pct": round(total_ret, 4),
        "annual_return_pct": round(ann, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(mdd * 100, 4),
        "n_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gp / gl, 3) if gl > 0 else (None if not gp else 999.0),
        "avg_hold_bars": round(sum(t["hold_bars"] for t in trades) / len(trades), 2)
                          if trades else 0.0,
        "exposure_pct": round(in_market_bars / max(1, nb) * 100, 2),
        "bars_per_year": bpy,
        "bars": nb,
    }
    return {"metrics": metrics, "trades": trades,
            "equity": equity_out, "params": p}


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{}")
    json.dump(run_backtest(payload), sys.stdout, ensure_ascii=False)
