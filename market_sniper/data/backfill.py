# -*- coding: utf-8 -*-
"""CLI 回填脚本。

用法：
    python -m market_sniper.data.backfill                  # 全市场默认候选池，日K
    python -m market_sniper.data.backfill --days 365        # 最近一年
    python -m market_sniper.data.backfill --period max      # 全历史
    python -m market_sniper.data.backfill --market HK       # 单市场
    python -m market_sniper.data.backfill --minute 1m       # 拉分钟K
    python -m market_sniper.data.backfill --symbols HK:00700,US:AAPL  # 指定标的
    python -m market_sniper.data.backfill --force           # 强制重拉
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from market_sniper.data import db, fetcher

log = logging.getLogger("market_sniper.backfill")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="回填行情")
    p.add_argument("--market", choices=("HK", "US", "CRYPTO"),
                   help="限定市场")
    p.add_argument("--symbols", help="逗号分隔的本地代码（HK:00700,US:AAPL）")
    p.add_argument("--days", type=int, help="拉最近 N 天日K")
    p.add_argument("--period", help="yfinance period (max/5y/1y...)")
    p.add_argument("--minute", choices=("1m", "5m", "15m", "30m", "60m", "1h"),
                   help="拉分钟K，period 固定为 7d/60d")
    p.add_argument("--force", action="store_true", help="忽略本地最新日期强制重拉")
    p.add_argument("--no-stocks", action="store_true", help="不更新 stocks 表")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    markets = (args.market,) if args.market else ("HK", "US", "CRYPTO")
    if not args.no_stocks:
        for m in markets:
            fetcher.ensure_stocks(m)

    if args.symbols:
        targets = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        targets = []
        for m in markets:
            targets.extend(f"{m}:{c}" for c, _ in fetcher.universes(m))

    t0 = time.time()
    n_daily = 0
    n_min = 0
    for local in targets:
        try:
            if args.minute:
                n = fetcher.backfill_minute(local, timeframe=args.minute,
                                            period="7d")
                n_min += n
            else:
                kwargs = {}
                if args.days:
                    kwargs["days"] = args.days
                if args.period:
                    kwargs["period"] = args.period
                if args.force:
                    kwargs["force"] = True
                n = fetcher.backfill_daily(local, **kwargs)
                n_daily += n
        except Exception as e:
            log.error("%s 失败: %s", local, e)
            continue

    dt = time.time() - t0
    print(f"\n完成：用时 {dt:.1f}s，日K +{n_daily} 条，分钟K +{n_min} 条")
    print(db.health())
    return 0


if __name__ == "__main__":
    sys.exit(main())