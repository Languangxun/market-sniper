#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market-sniper 入口。

用法：
    python main.py                # 启动 GUI
    python main.py --backfill     # 先跑一遍 HK/US/Crypto 默认候选池回填，再启动 GUI
    python main.py --health       # 打印 DB 健康度
"""
from __future__ import annotations

import argparse
import logging
import sys
import time


def cmd_health() -> int:
    from market_sniper.data import db
    import json
    print(json.dumps(db.health(), ensure_ascii=False, indent=2))
    return 0


def cmd_backfill(markets: list[str]) -> int:
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from market_sniper.data import fetcher
    for m in markets:
        fetcher.ensure_stocks(m)
        for code, _ in fetcher.universes(m):
            try:
                fetcher.backfill_daily(f"{m}:{code}", days=120)
            except Exception as e:
                print(f"  {m}:{code} failed: {e}", file=sys.stderr)
    return 0


def cmd_gui() -> int:
    from market_sniper.gui.main_window import main
    return main()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="market-sniper")
    p.add_argument("--backfill", action="store_true",
                   help="先跑一遍默认候选池回填")
    p.add_argument("--health", action="store_true", help="打印 DB 健康度")
    p.add_argument("--market", action="append", default=[],
                   help="限定回填市场（可多次）")
    args = p.parse_args(argv)

    if args.health:
        return cmd_health()

    if args.backfill:
        markets = args.market or ["HK", "US", "CRYPTO"]
        cmd_backfill(markets)

    return cmd_gui()


if __name__ == "__main__":
    sys.exit(main())