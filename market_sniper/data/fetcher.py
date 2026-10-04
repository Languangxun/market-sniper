# -*- coding: utf-8 -*-
"""统一 fetcher：四市场按 code 路由到对应 source，写入 SQLite。

公开 API：
    backfill_daily(local, *, days=None, period=None, force=False) -> int
        days/period 二选一：days=730 表示拉最近 730 天；period='max' 表示全历史。
        force=True 强制重拉（不接续）。
    backfill_minute(local, *, timeframe='1m', period='7d') -> int
    quote(local) -> dict
        当前最新价（仅内存查询，不入库）
    universes(market) -> list[dict]
        返回指定市场的默认候选池（首次全量回填时使用）。
"""
from __future__ import annotations

import datetime
import logging
import time

from market_sniper import config as config_mod
from market_sniper import symbols as sym
from market_sniper.data import db
from market_sniper.data.sources import ccxt_crypto, tencent_hk_us, yfinance_us

_cc = ccxt_crypto

log = logging.getLogger("market_sniper.fetcher")


# 候选默认池（首次回填用）
DEFAULT_UNIVERSE = {
    "HK": [
        ("00700", "腾讯控股"), ("09988", "阿里巴巴-W"),
        ("03690", "美团-W"), ("01810", "小米集团-W"),
        ("02318", "中国平安"), ("01211", "比亚迪股份"),
        ("00939", "建设银行"), ("01398", "工商银行"),
        ("00388", "香港交易所"), ("00005", "汇丰控股"),
        ("00941", "中国移动"), ("02020", "安踏体育"),
        ("01024", "快手-W"), ("09618", "京东集团-SW"),
        ("09961", "携程集团-S"), ("02628", "中国人寿"),
        ("02269", "药明生物-W"),
    ],
    "US": [
        ("AAPL", "Apple Inc."), ("MSFT", "Microsoft"),
        ("GOOGL", "Alphabet A"), ("AMZN", "Amazon"),
        ("NVDA", "NVIDIA"), ("META", "TSLA_PLACEHOLDER"),  # will fix below
        ("TSLA", "Tesla"), ("NFLX", "Netflix"),
        ("AMD", "AMD"), ("INTC", "Intel"),
        ("BABA", "Alibaba ADR"), ("PDD", "PDD Holdings"),
        ("NIO", "NIO"), ("XOM", "Exxon"),
        ("JPM", "JPMorgan"), ("V", "Visa"),
        ("MA", "Mastercard"), ("DIS", "Disney"),
        ("COIN", "Coinbase"), ("MSTR", "MicroStrategy"),
    ],
    "CRYPTO": [
        ("BTC/USDT", "Bitcoin"), ("ETH/USDT", "Ethereum"),
        ("SOL/USDT", "Solana"), ("BNB/USDT", "BNB"),
        ("XRP/USDT", "XRP"), ("DOGE/USDT", "Dogecoin"),
        ("ADA/USDT", "Cardano"), ("AVAX/USDT", "Avalanche"),
        ("TRX/USDT", "TRON"), ("DOT/USDT", "Polkadot"),
        ("LINK/USDT", "Chainlink"), ("POL/USDT", "Polygon"),
        ("LTC/USDT", "Litecoin"), ("BCH/USDT", "Bitcoin Cash"),
        ("ATOM/USDT", "Cosmos"), ("ETC/USDT", "Ethereum Classic"),
        ("UNI/USDT", "Uniswap"), ("APT/USDT", "Aptos"),
        ("ARB/USDT", "Arbitrum"), ("OP/USDT", "Optimism"),
    ],
}

# 修复 US 池 typo
DEFAULT_UNIVERSE["US"] = [
    (c, n) for c, n in DEFAULT_UNIVERSE["US"] if not n.endswith("_PLACEHOLDER")
] + [("META", "Meta Platforms")]


def _resolve_universe(market: str) -> list[tuple[str, str]]:
    """返回 [(code, name), ...]；若数据库已有 stocks，则与默认池并集。"""
    default = DEFAULT_UNIVERSE.get(market, [])
    existing = {r["code"] for r in db.list_universe(market)}
    out = [(c, n) for c, n in default if c not in existing]
    # 已入库的也保留
    return out


# ---------------- 路由 ----------------
# Yahoo range=max 会降采样（MSFT 只回 ~164 根），max 必须走显式起止
_MAX_START = "1970-01-01"


def _route_daily(code: str, market: str, *, period: str = "max",
                 start: str | None = None, end: str | None = None):
    if market == "HK":
        if not start and period == "max":
            start = _MAX_START
            end = datetime.date.today().strftime("%Y-%m-%d")
        return yfinance_us.fetch_daily(sym.yf_symbol(f"HK:{code}"),
                                       period=period, start=start, end=end)
    if market == "US":
        if not start and period == "max":
            start = _MAX_START
            end = datetime.date.today().strftime("%Y-%m-%d")
        return yfinance_us.fetch_daily(code, period=period,
                                       start=start, end=end)
    if market == "CRYPTO":
        # 日K拉 max 走 ccxt（按 since_ms=2010-01-01）
        if period == "max":
            since = _cc.dt_to_ms("2010-01-01")
            return _cc.fetch_ohlcv_range(code, timeframe="1d",
                                          since_ms=since, until_ms=None)
        # 否则用 start/end 区间
        since = _cc.dt_to_ms(start) if start else _cc.dt_to_ms("2010-01-01")
        until = _cc.dt_to_ms(end) if end else None
        return _cc.fetch_ohlcv_range(code, timeframe="1d",
                                     since_ms=since, until_ms=until)
    raise ValueError(f"unknown market: {market}")


def _route_minute(code: str, market: str, *, interval: str, period: str):
    if market == "HK":
        return yfinance_us.fetch_minute(sym.yf_symbol(f"HK:{code}"),
                                         interval=interval, period=period)
    if market == "US":
        return yfinance_us.fetch_minute(code, interval=interval, period=period)
    if market == "CRYPTO":
        # yfinance minute bar 默认 '7d'；crypto 给 1000 根即可（约 16.7 小时）
        return _cc.fetch_ohlcv(code, timeframe=interval, limit=1000)
    raise ValueError(f"unknown market: {market}")


def _route_quote(code: str, market: str):
    if market in ("HK", "US"):
        src = str(config_mod.get_config().get(f"sources.{market}",
                                              "yfinance")).lower()
        if src in ("tencent", "tq", "qq"):
            return tencent_hk_us.fetch_quote(market, code)
    if market == "HK":
        return yfinance_us.fetch_quote(sym.yf_symbol(f"HK:{code}"))
    if market == "US":
        return yfinance_us.fetch_quote(code)
    if market == "CRYPTO":
        ex = _cc.get_ex(_cc.exchange_order()[0])
        t = ex.fetch_ticker(code)
        return {
            "last": t.get("last"), "prev_close": t.get("previousClose"),
            "day_high": t.get("high"), "day_low": t.get("low"),
            "volume": t.get("baseVolume"),
            "name": t.get("info", {}).get("symbol"),
        }
    return None


# ---------------- 公共 API ----------------
def quote(local: str) -> dict | None:
    market, code = local.split(":", 1)
    return _route_quote(code, market.upper())


def backfill_daily(local: str, *, days: int | None = None,
                   period: str | None = None, force: bool = False,
                   upsert_meta: bool = True) -> int:
    """拉取并入库日K。

    days 给定时：拉最近 N 天。
    period 给定时：例如 'max' / '5y' / '1y'。
    二者都给时优先 days。

    返回入库条数。force=True 时忽略 last_date 重新拉取。
    """
    market, code = local.split(":", 1)
    market = market.upper()
    last = db.last_date(market, code) if not force else None

    if period is None and days is None:
        period = "max"
    if days:
        # 用 start/end 区间拉
        if last:
            start = last
        else:
            start = (datetime.date.today() -
                     datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.date.today().strftime("%Y-%m-%d")
        rows = _route_daily(code, market, start=start, end=end)
    else:
        rows = _route_daily(code, market, period=period)

    if not rows:
        log.info("%s 无新K线", local)
        return 0
    # 转换为 daily_bars 格式（crypto + yfcc 含 'date'，已是 YYYY-MM-DD）
    if market == "CRYPTO":
        # ts -> date 已是 YYYY-MM-DD
        formatted = [r for r in rows]  # schema 已兼容
    else:
        formatted = rows

    n = db.upsert_daily_bars(market, code, formatted, source="auto")
    if upsert_meta:
        db.set_meta(f"last_daily:{local}",
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("%s backfilled %d daily bars", local, n)
    return n


def period_for_bars(timeframe: str, bars: int) -> str:
    """按想要的根数近似映射 yfinance period（Yahoo 分钟K 只收固定档）。"""
    tf = timeframe
    if tf == "1m":
        return "7d"                       # yfinance 1m 上限 7d
    days = max(1, int(bars) // 60 + (1 if int(bars) % 60 else 0))
    if tf in ("5m", "15m", "30m"):
        for limit, p in ((1, "1d"), (5, "5d"), (30, "1mo"), (60, "60d")):
            if days <= limit:
                return p
        return "60d"
    if tf in ("60m", "1h"):
        for limit, p in ((7, "7d"), (30, "1mo"), (90, "3mo"),
                         (180, "6mo"), (365, "1y"), (730, "2y")):
            if days <= limit:
                return p
        return "730d"
    return "7d"


def backfill_minute(local: str, *, timeframe: str = "1m",
                    period: str = "7d", bars: int | None = None) -> int:
    """拉取并入库分钟K。

    period：yfinance 档位（'7d'/'60d'/'730d'...）。
    bars：想要的目标根数——crypto 直接 limit=bars；yfinance 换算成最近档位。
    """
    market, code = local.split(":", 1)
    market = market.upper()
    if bars:
        if market == "CRYPTO":
            rows = _route_minute_bars_crypto(code, timeframe, bars)
        else:
            period = period_for_bars(timeframe, bars)
            rows = _route_minute(code, market, interval=timeframe, period=period)
    else:
        rows = _route_minute(code, market, interval=timeframe, period=period)
    if not rows:
        log.info("%s 无分钟K线", local)
        return 0
    n = db.upsert_min_bars(market, code, timeframe, rows, source="auto")
    db.set_meta(f"last_min:{local}:{timeframe}",
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("%s %s backfilled %d bars", local, timeframe, n)
    return n


def _route_minute_bars_crypto(code: str, timeframe: str, bars: int) -> list[dict]:
    tf = "1h" if timeframe == "60m" else timeframe
    return _cc.fetch_ohlcv(code, timeframe=tf, limit=max(1, min(bars, 1000)))


def universes(market: str) -> list[tuple[str, str]]:
    """返回指定市场的全量候选 [(code, name), ...] = DB 已存 ∪ 默认池。

    用于回填脚本批量遍历。
    """
    m = market.upper()
    default = DEFAULT_UNIVERSE.get(m, [])
    existing = {r["code"]: r.get("name") or "" for r in db.list_universe(m)}
    out = [(c, existing.get(c, "")) for c in existing]
    for c, n in default:
        if c not in existing:
            out.append((c, n))
    # 排序稳定
    out.sort(key=lambda x: x[0])
    return out


def ensure_stocks(market: str):
    """把默认候选池先 upsert 到 stocks。"""
    m = market.upper()
    for code, name in _resolve_universe(m):
        db.upsert_stock(m, code, name=name)
        # 派生 exchange
        if m == "CRYPTO":
            base, _, _ = code.partition("/")
            db.upsert_stock(m, code, name=name, exchange="Binance", note=base)
        elif m == "HK":
            db.upsert_stock(m, code, name=name, exchange="HKEX")
        elif m == "US":
            db.upsert_stock(m, code, name=name, exchange="NASDAQ/NYSE/AMEX")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_stocks("HK")
    backfill_daily("HK:00700", days=30)
    print(db.load_daily_bars("HK", "00700", limit=5))