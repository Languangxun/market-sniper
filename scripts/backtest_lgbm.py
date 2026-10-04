#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全库批量回测（港股 LightGBM 信号）。

- 信号: market_sniper.lgbm_model.run_algo（日线/分时按周期）
- 成交: compute/backtest.py（下一根开盘成交、双边手续费+滑点、全仓、HK 只多）
- 输出: 控制台汇总 + data/reports/ 每标的明细（csv/xlsx），含买入持有对比

用法：
    python3 scripts/backtest_lgbm.py                          # 日线全库
    python3 scripts/backtest_lgbm.py --timeframe intraday      # 5m 全库
    python3 scripts/backtest_lgbm.py --timeframe all --limit 50
    python3 scripts/backtest_lgbm.py --symbols HK:00700,HK:03690 --out /tmp/bt.xlsx
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sniper import lgbm_model
from market_sniper.data import db

log = logging.getLogger("market_sniper.backtest_lgbm")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(_ROOT, "data", "reports")

_TF_CFG = {
    "daily":    {"native": "1d", "min_bars": 250},
    "intraday": {"native": "5m", "min_bars": 500},
}


def _load_engine():
    path = os.path.join(_ROOT, "compute", "backtest.py")
    spec = importlib.util.spec_from_file_location("ms_bt_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _industry_map() -> dict:
    import json
    try:
        with open(os.path.join(_ROOT, "data", "hk_industry.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def backtest_one(code: str, kind: str, name: str, industry: str,
                 engine, params: dict, start: str | None,
                 full: bool, min_turnover: float) -> dict | None:
    if kind == "daily":
        bars = db.load_daily_bars("HK", code)
    else:
        bars = db.load_min_bars("HK", code, "5m")
    if not bars or len(bars["close"]) < _TF_CFG[kind]["min_bars"]:
        return None
    out = lgbm_model.run_algo(bars, market="HK", timeframe=kind,
                              max_signals=1_000_000)
    signals = out.get("signals") or []
    win = start if start is not None else (None if full else out.get("valid_from"))
    if win:
        dates = bars.get("dates") or bars.get("ts") or []
        lo = 0
        while lo < len(dates) and str(dates[lo])[:10] < win:
            lo += 1
        if len(dates) - lo < 60:
            return None
        bars = dict(bars)
        for k in ("dates", "ts", "open", "high", "low", "close",
                  "volume", "amount"):
            if k in bars and bars[k] is not None:
                bars[k] = bars[k][lo:]
        signals = [s for s in signals if str(s.get("ts"))[:10] >= win]
    close = np.asarray(bars["close"], dtype=np.float64)
    if min_turnover > 0:
        turn = np.median(close * np.asarray(bars["volume"], dtype=np.float64))
        if not np.isfinite(turn) or turn < min_turnover:
            return None
    res = engine.run_backtest({
        "market": "HK", "tf": _TF_CFG[kind]["native"],
        "bars": bars, "signals": signals, "params": params,
    })
    m = res["metrics"]
    bh = (close[-1] / close[0] - 1) * 100 if len(close) > 1 else 0.0
    return {
        "code": code, "name": name, "industry": industry,
        "bars": m["bars"], "start": str((bars.get("dates") or bars.get("ts"))[0])[:10],
        "model": out.get("model_id"), "auc": out.get("auc"),
        "position": out.get("position"),
        "trades": m["n_trades"], "win_rate": m["win_rate_pct"],
        "total_return": m["total_return_pct"],
        "annual_return": m["annual_return_pct"],
        "sharpe": m["sharpe"], "max_dd": m["max_drawdown_pct"],
        "exposure": m["exposure_pct"], "profit_factor": m["profit_factor"],
        "avg_hold": m["avg_hold_bars"], "buy_hold": round(bh, 4),
        "excess": round(m["total_return_pct"] - bh, 4),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {}
    def vals(k):
        return [r[k] for r in rows if r.get(k) is not None]
    def q(k, p, default=0.0):
        v = vals(k)
        return round(float(np.quantile(v, p)), 2) if v else default
    traded = [r for r in rows if r["trades"] > 0]
    return {
        "symbols": len(rows),
        "traded": len(traded),
        "traded_pct": round(100 * len(traded) / len(rows), 2),
        "trades": sum(r["trades"] for r in rows),
        "profitable_pct": round(100 * sum(1 for r in rows if r["total_return"] > 0) / len(rows), 2),
        "beat_bh_pct": round(100 * sum(1 for r in rows if r["excess"] > 0) / len(rows), 2),
        "return_mean": round(float(np.mean(vals("total_return"))), 2)
        if vals("total_return") else 0.0,
        "return_median": q("total_return", 0.5),
        "return_p25": q("total_return", 0.25),
        "return_p75": q("total_return", 0.75),
        "buy_hold_median": q("buy_hold", 0.5),
        "win_rate_median": q("win_rate", 0.5),
        "sharpe_median": q("sharpe", 0.5, 0.0),
        "max_dd_median": q("max_dd", 0.5, 0.0),
    }


def write_report(rows: list[dict], out: str) -> None:
    import pandas as pd

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df = pd.DataFrame(rows).sort_values("total_return", ascending=False)
    if out.endswith(".xlsx"):
        df.to_excel(out, index=False)
    else:
        df.to_csv(out, index=False, encoding="utf-8-sig")
    log.info("明细已写出 %s（%d 行）", out, len(df))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全库 LightGBM 回测（港股）")
    ap.add_argument("--timeframe", choices=("daily", "intraday", "all"),
                    default="daily")
    ap.add_argument("--symbols", help="逗号分隔 HK:00700,...；缺省全库")
    ap.add_argument("--limit", type=int, default=0, help="最多 N 只（调试）")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--fee-bps", type=float, default=5)
    ap.add_argument("--slippage-bps", type=float, default=2)
    ap.add_argument("--position-pct", type=float, default=1.0)
    ap.add_argument("--min-turnover", type=float, default=300_000,
                    help="窗口内中位日成交额下限（HKD，默认 30 万，0=不过滤）")
    ap.add_argument("--include-unprofiled", action="store_true",
                    help="含无行业档案标的（债券/仙股等，默认剔除）")
    ap.add_argument("--start", help="回测起始日 YYYY-MM-DD（默认=模型验证集起点，样本外）")
    ap.add_argument("--full", action="store_true",
                    help="全历史（含训练期，样本内，仅参考）")
    ap.add_argument("--out", help="明细输出 .csv/.xlsx（缺省自动命名）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    lgbm_model._retrain_last_check = time.time() + 86400   # 批量回测不触发重训
    engine = _load_engine()
    ind_map = _industry_map()

    if args.symbols:
        codes = [s.split(":", 1)[-1].strip() for s in args.symbols.split(",") if s.strip()]
    else:
        codes = [r["code"] for r in db.list_universe("HK")]
        if ind_map and not args.include_unprofiled:
            codes = [c for c in codes if c in ind_map]
    if args.limit:
        codes = codes[:args.limit]
    names = {r["code"]: (r.get("name") or "") for r in db.list_universe("HK")}
    log.info("标的 %d 只（成交额下限 %.0f）", len(codes), args.min_turnover)

    kinds = ("daily", "intraday") if args.timeframe == "all" else (args.timeframe,)
    params = {"fee_bps": args.fee_bps, "slippage_bps": args.slippage_bps,
              "position_pct": args.position_pct, "initial_cash": 1_000_000}
    stamp = time.strftime("%Y%m%d_%H%M")
    rc = 0
    for kind in kinds:
        t0 = time.time()
        rows, failed = [], 0
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = {ex.submit(backtest_one, c, kind, names.get(c, ""),
                              (ind_map.get(c) or {}).get("industry") or "",
                              engine, params, args.start, args.full,
                              args.min_turnover): c
                    for c in codes}
            for i, fut in enumerate(as_completed(futs), 1):
                code = futs[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    failed += 1
                    log.warning("%s 回测失败: %s", code, e)
                    continue
                if row:
                    rows.append(row)
                if i % 200 == 0:
                    log.info("进度 %d/%d（有效 %d）", i, len(codes), len(rows))
        s = summarize(rows)
        log.info("==== %s 汇总（%.0fs）: %s", kind, time.time() - t0, s)
        if not rows:
            rc = 1
            continue
        top_pool = [r for r in rows if r["trades"] >= 5] or rows
        top = sorted(top_pool, key=lambda r: -r["total_return"])[:10]
        log.info("收益前 10（交易≥5）：")
        for r in top:
            log.info("  %s %-8s 收益 %7.2f%% 夏普 %6.2f 回撤 %7.2f%% "
                     "交易 %3d 胜率 %5.1f%% 基准 %7.2f%%",
                     r["code"], r["name"][:8], r["total_return"], r["sharpe"],
                     r["max_dd"], r["trades"], r["win_rate"], r["buy_hold"])
        out = args.out or os.path.join(REPORT_DIR, f"lgbm_{kind}_{stamp}.csv")
        if args.out and len(kinds) > 1:
            base, ext = os.path.splitext(args.out)
            out = f"{base}_{kind}{ext}"
        write_report(rows, out)
        if failed:
            log.warning("%s 失败 %d 只", kind, failed)
    return rc


if __name__ == "__main__":
    sys.exit(main())
