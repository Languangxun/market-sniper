# -*- coding: utf-8 -*-
"""Stooq 日K整包导入：解压 → 清洗 → 写 daily_bars / stocks。

先在浏览器打开 https://stooq.com/db/h/ 过验证码下载：
    d_hk_txt.zip (~105MB)  港股日K
    d_us_txt.zip (~515MB)  美股日K
放到 data/raw/ 后运行：

    python3 scripts/import_stooq.py                  # 自动扫描 data/raw/，每标的留最近 1000 根
    python3 scripts/import_stooq.py --timeframe 5m   # 只导 5 分K（时间戳华沙→UTC）
    python3 scripts/import_stooq.py --bars 0         # 保留全历史
    python3 scripts/import_stooq.py --types all      # 含 ETF/债券等（默认只导 stocks）
    python3 scripts/import_stooq.py --overwrite      # 已有K线的标的也覆盖（默认跳过）
    python3 scripts/import_stooq.py --active-days 30 # 只要最近 30 天有行情的标的
    python3 scripts/import_stooq.py --dry-run        # 只统计不落库
    python3 scripts/import_stooq.py --zip data/raw/d_hk_txt.zip --market HK

Stooq ASCII 格式：<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
只取 PER=D 的日线；标的映射：AAPL.US -> US:AAPL，0700.HK -> HK:00700。
"""
from __future__ import annotations

import argparse
import datetime
import glob
import io
import logging
import os
import sys
import time
import zipfile
from collections import deque
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_sniper.data import db

log = logging.getLogger("market_sniper.import_stooq")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RAW_DIR = os.path.join(_ROOT, "data", "raw")


def discover(raw_dir: str, explicit: list[str], market: str | None) -> list[str]:
    """返回待解析文件；未显式给 --zip 时按文件名 hk/us 自动识别市场。"""
    if explicit:
        out: list[str] = []
        for pat in explicit:
            hit = sorted(glob.glob(pat))
            if not hit:
                log.error("找不到文件: %s", pat)
            out.extend(hit)
        return out
    out = []
    for p in sorted(glob.glob(os.path.join(raw_dir, "*.zip")) +
                    glob.glob(os.path.join(raw_dir, "*.txt"))):
        low = os.path.basename(p).lower()
        if market and market.lower() not in low:
            continue
        if not market and not any(k in low for k in ("hk", "us")):
            log.warning("跳过无法识别市场的文件: %s", p)
            continue
        out.append(p)
    return out


def iter_lines(paths: list[str], types: tuple[str, ...] = ()):
    """types 非空时按压缩包内目录名过滤，如 'stocks' / 'etfs'。"""
    for p in paths:
        if p.lower().endswith(".zip"):
            with zipfile.ZipFile(p) as zf:
                members = [i for i in zf.infolist()
                           if not i.is_dir()
                           and i.filename.lower().endswith((".txt", ".csv"))]
                if types:
                    members = [i for i in members
                               if any(f" {t}/" in i.filename.lower()
                                      for t in types)]
                log.info("解析 %s：%d 个文件 / %.0f MB",
                         os.path.basename(p), len(members),
                         sum(i.file_size for i in members) / 1e6)
                for info in members:
                    with zf.open(info) as fh:
                        yield from io.TextIOWrapper(fh, encoding="utf-8",
                                                    errors="replace")
        else:
            log.info("解析 %s (%.0f MB)", os.path.basename(p),
                     os.path.getsize(p) / 1e6)
            with open(p, encoding="utf-8", errors="replace") as fh:
                yield from fh


def map_ticker(ticker: str) -> tuple[str, str] | None:
    t = ticker.strip().strip('"')
    up = t.upper()
    if up.endswith(".US"):
        base = t[:-3].strip()
        if not base or base[0] in "^$":
            return None
        return "US", base.replace(".", "-").upper()
    if up.endswith(".HK"):
        base = t[:-3].strip()
        digits = "".join(ch for ch in base if ch.isdigit())
        return ("HK", digits.zfill(5)) if digits else None
    return None


_WARSAW = ZoneInfo("Europe/Warsaw")   # Stooq 分钟线时间戳为华沙本地时间


def _to_utc(raw_date: str, raw_time: str) -> str | None:
    try:
        dt = datetime.datetime.strptime(raw_date + raw_time, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    utc = dt.replace(tzinfo=_WARSAW).astimezone(datetime.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_line(line: str):
    """返回 (kind, market, code, row) 或 None；kind = 'daily' | '5m'。"""
    if not line or line.startswith("<"):
        return None
    parts = line.rstrip("\r\n").split(",")
    if len(parts) < 9:
        return None
    mapped = map_ticker(parts[0])
    if not mapped:
        return None
    per = parts[1].strip().strip('"').upper()
    raw_date = parts[2].strip().strip('"')
    if len(raw_date) != 8 or not raw_date.isdigit():
        return None
    if per not in ("D", "5"):
        return None
    try:
        row = {
            "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            "open": float(parts[4]), "high": float(parts[5]),
            "low": float(parts[6]), "close": float(parts[7]),
            "volume": float(parts[8]),
        }
    except (TypeError, ValueError):
        return None
    if per == "D":
        return "daily", mapped[0], mapped[1], row
    ts = _to_utc(raw_date, parts[3].strip().strip('"'))
    if not ts:
        return None
    row["ts"] = ts
    return "5m", mapped[0], mapped[1], row


def scan_max_date(paths: list[str], types: tuple[str, ...] = ()) -> str | None:
    mx = None
    for line in iter_lines(paths, types):
        if len(line) < 12 or line[0] == "<":
            continue
        parts = line.split(",", 3)
        if len(parts) < 3 or parts[1].strip() not in ("D", "5"):
            continue
        d = parts[2].strip()
        if len(d) == 8 and d.isdigit() and (mx is None or d > mx):
            mx = d
    return f"{mx[:4]}-{mx[4:6]}-{mx[6:]}" if mx else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stooq 日K整包导入")
    p.add_argument("--zip", action="append", default=[],
                   help="压缩包/文本路径，可多次；缺省扫描 data/raw/")
    p.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    p.add_argument("--market", choices=("HK", "US"), help="限定市场")
    p.add_argument("--bars", type=int, default=1000,
                   help="每标的保留最近 N 根（0=全历史，默认 1000）")
    p.add_argument("--min-bars", type=int, default=60,
                   help="少于 N 根的标的跳过（默认 60）")
    p.add_argument("--active-days", type=int, default=30,
                   help="最后交易日距今超过 N 天视为不活跃（0=不过滤，默认 30）")
    p.add_argument("--include-delisted", action="store_true",
                   help="不按活跃度过滤；不活跃标的标记 delisted 后仍入库")
    p.add_argument("--types", default="stocks",
                   help="压缩包内目录过滤：stocks / stocks,etfs / all（默认 stocks）")
    p.add_argument("--timeframe", choices=("daily", "5m"),
                   help="只导某类（缺省自动识别包内 PER）")
    p.add_argument("--minute-bars", type=int, default=0,
                   help="5m 每标的保留最近 N 根（0=全部，默认）")
    p.add_argument("--overwrite", action="store_true",
                   help="已有日K的标的也覆盖（默认跳过，避免与 Yahoo 复权价拼接跳空）")
    p.add_argument("--limit", type=int, default=0, help="最多导入 N 个标的（调试）")
    p.add_argument("--dry-run", action="store_true", help="只统计不落库")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    paths = discover(args.raw_dir, args.zip, args.market)
    if not paths:
        log.error("data/raw/ 下没有待导入文件；先下载 d_hk_txt.zip / d_us_txt.zip")
        return 1
    log.info("待导入 %d 个文件: %s", len(paths),
             ", ".join(os.path.basename(x) for x in paths))
    types = tuple(t.strip().lower() for t in args.types.split(",")
                  if t.strip() and t.strip().lower() != "all")

    max_date = scan_max_date(paths, types)
    ref = max(datetime.date.today().isoformat(), max_date or "")
    cutoff = None
    if args.active_days > 0:
        cutoff = (datetime.date.fromisoformat(ref) -
                  datetime.timedelta(days=args.active_days)).isoformat()
    log.info("数据最新日期 %s，活跃截止 %s", max_date, cutoff or "不过滤")

    stats = {"symbols": 0, "imported": 0, "bars": 0, "min_imported": 0,
             "min_bars": 0, "inactive": 0, "sparse": 0, "unmapped": 0,
             "existing": 0}
    t0 = time.time()
    cur_key: tuple | None = None
    cur_kind = "daily"
    buf: deque | list = []

    def flush():
        if cur_key is None:
            return
        stats["symbols"] += 1
        _kind, market, code, _ticker = cur_key
        rows = list(buf)
        if not rows:
            stats["unmapped"] += 1
            return
        rows.sort(key=lambda r: r["ts"] if _kind == "5m" else r["date"])
        if args.market and market != args.market:
            return
        last = rows[-1]["date"]
        if cutoff and last < cutoff:
            stats["inactive"] += 1
            if not args.include_delisted:
                return
        if len(rows) < args.min_bars:
            stats["sparse"] += 1
            return
        active = not cutoff or last >= cutoff
        exchange = "HKEX" if market == "HK" else None
        if _kind == "daily":
            if not args.overwrite and db.count_bars(market, code) > 0:
                stats["existing"] += 1
                if not args.dry_run:
                    db.upsert_stock(market, code, exchange=exchange,
                                    listed=active, delisted=not active,
                                    note="stooq")
                return
            if args.limit and stats["imported"] >= args.limit:
                return
            if args.dry_run:
                stats["bars"] += len(rows)
            else:
                stats["bars"] += db.upsert_daily_bars(market, code, rows,
                                                     source="stooq")
                db.upsert_stock(market, code, exchange=exchange,
                                listed=active, delisted=not active, note="stooq")
            stats["imported"] += 1
            if stats["imported"] % 200 == 0:
                log.info("已导入日K %d 标的 / %d 根 ...",
                         stats["imported"], stats["bars"])
        else:
            if args.limit and stats["min_imported"] >= args.limit:
                return
            batch_last = rows[-1]["ts"]
            if not args.overwrite:
                with db.db_conn() as cx:
                    r = cx.execute(
                        "SELECT MAX(ts) FROM min_bars WHERE market=? AND code=? "
                        "AND timeframe='5m' AND source='stooq'",
                        (market, code)).fetchone()
                if r and r[0] and r[0] >= batch_last:
                    stats["existing"] += 1
                    return
            if args.dry_run:
                stats["min_bars"] += len(rows)
            else:
                stats["min_bars"] += db.upsert_min_bars(market, code, "5m",
                                                       rows, source="stooq")
                db.upsert_stock(market, code, exchange=exchange,
                                listed=active, delisted=not active, note="stooq")
            stats["min_imported"] += 1
            if stats["min_imported"] % 200 == 0:
                log.info("已导入5m %d 标的 / %d 根 ...",
                         stats["min_imported"], stats["min_bars"])

    stop = False
    for line in iter_lines(paths, types):
        parsed = parse_line(line)
        if parsed is None:
            continue
        kind, market, code, row = parsed
        if args.timeframe and kind != args.timeframe:
            continue
        ticker = line.split(",", 1)[0].strip()
        key = (kind, market, code, ticker)
        if key != cur_key:
            flush()
            if args.limit and (stats["imported"] >= args.limit
                               or stats["min_imported"] >= args.limit):
                stop = True
                break
            cur_key = key
            cur_kind = kind
            cap = args.bars if kind == "daily" else args.minute_bars
            buf = deque(maxlen=cap) if cap > 0 else []
        buf.append(row)  # type: ignore[union-attr]
    if not stop:
        flush()

    dt = time.time() - t0
    log.info("完成 %.1fs：标的 %d（日K导入 %d / 已有 %d / 5m导入 %d / "
             "不活跃 %d / 样本不足 %d），日K +%d，5m +%d%s",
             dt, stats["symbols"], stats["imported"], stats["existing"],
             stats["min_imported"], stats["inactive"], stats["sparse"],
             stats["bars"], stats["min_bars"],
             "（dry-run）" if args.dry_run else "")
    if not args.dry_run:
        log.info("health: %s", db.health())
    return 0


if __name__ == "__main__":
    sys.exit(main())
