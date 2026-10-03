# -*- coding: utf-8 -*-
"""全量回填：三市场候选池 日K全历史 + 1m/5m/15m/30m/60m 分钟K尽量多。

后台运行：
    nohup python3 scripts/backfill_all.py > logs/backfill_all.log 2>&1 &
    tail -f logs/backfill_all.log

量级（yfinance 免费源上限）：
    日K      全历史（period=max）
    1m       HK/US 最近 7d；CRYPTO 最近 30d（分页）
    5m       HK/US 最近 60d；CRYPTO 最近 90d
    15m      HK/US 最近 60d；CRYPTO 最近 180d
    30m      HK/US 最近 60d；CRYPTO 最近 1y
    60m      HK/US 最近 730d；CRYPTO 最近 3y
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sniper.data import db, fetcher
from market_sniper.data.sources import ccxt_crypto as cc

log = logging.getLogger("market_sniper.backfill_all")

# tf -> (yfinance period, crypto 回看天数)
MIN_PLAN = {
    "1m":  ("7d",   30),
    "5m":  ("60d",  90),
    "15m": ("60d",  180),
    "30m": ("60d",  365),
    "60m": ("730d", 1095),
}
_CC_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h"}


def crypto_minute(code: str, tf: str, days: int) -> int:
    since = cc.dt_to_ms((datetime.date.today() -
                         datetime.timedelta(days=days)).strftime("%Y-%m-%d"))
    rows = cc.fetch_ohlcv_range(code, timeframe=_CC_TF[tf], since_ms=since)
    if not rows:
        return 0
    return db.upsert_min_bars("CRYPTO", code, tf, rows, source="ccxt")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    stats = {"daily": 0, "min": 0, "fail": 0}
    for m in ("HK", "US", "CRYPTO"):
        fetcher.ensure_stocks(m)
        targets = [f"{m}:{c}" for c, _ in fetcher.universes(m)]
        log.info("==== %s：%d 标的 ====", m, len(targets))

        # 1) 日K 全历史
        for local in targets:
            try:
                n = fetcher.backfill_daily(local, period="max")
                stats["daily"] += n
                log.info("日K %s +%d", local, n)
            except Exception as e:
                stats["fail"] += 1
                log.error("日K %s 失败: %s", local, e)

        # 2) 分钟K 分档
        for tf, (yf_p, cdays) in MIN_PLAN.items():
            log.info("---- %s 分钟K %s ----", m, tf)
            for local in targets:
                try:
                    if m == "CRYPTO":
                        n = crypto_minute(local.split(":", 1)[1], tf, cdays)
                    else:
                        n = fetcher.backfill_minute(local, timeframe=tf,
                                                    period=yf_p)
                    stats["min"] += n
                    log.info("分钟 %s %s +%d", local, tf, n)
                except Exception as e:
                    stats["fail"] += 1
                    log.error("分钟 %s %s 失败: %s", local, tf, e)

    log.info("==== 完成 %.0fs：日K +%d / 分钟 +%d / 失败 %d ====",
             time.time() - t0, stats["daily"], stats["min"], stats["fail"])
    log.info("health: %s", db.health())
    return 0


if __name__ == "__main__":
    sys.exit(main())
