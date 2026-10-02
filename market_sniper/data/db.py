# -*- coding: utf-8 -*-
"""SQLite 数据层：统一表 `daily_bars(market, symbol, ...)`。

`local` 字段 = `market:symbol`（如 HK:00700 / US:AAPL / CRYPTO:BTC/USDT / CN:sh600000）。

表：
    stocks        元信息（market, code, name, exchange, listed, delisted）
    daily_bars    日K  (market, code, date, o/h/l/c, vol, amount)  UNIQUE(market,code,date)
    min_bars      分钟K(market, code, timeframe, ts, o/h/l/c, vol)  UNIQUE(market,code,timeframe,ts)
    meta          配置缓存 (key, value, ts)

约定：
    daily_bars.volume / amount 单位与源一致（crypto 成交量用基础币，A股用手）；
    min_bars.volume 同源；timeframe 取 '1m' / '5m' / '15m' / '30m' / '60m'。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_DEFAULT_DB = os.path.join(_ROOT, "data", "market_cache.db")
DB_PATH = os.environ.get("MARKET_SNIPER_DB", _DEFAULT_DB)

_lock = threading.Lock()
_tls = threading.local()


def db_path() -> str:
    return DB_PATH


def _cx() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _init_schema(conn)
        _tls.conn = conn
    return conn


@contextmanager
def db_conn():
    yield _cx()


def close():
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        conn.close()
        _tls.conn = None


def reopen():
    _tls.conn = None


# -------- schema --------
def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stocks (
            market   TEXT NOT NULL,
            code     TEXT NOT NULL,
            name     TEXT,
            exchange TEXT,
            listed   INTEGER DEFAULT 1,
            delisted INTEGER DEFAULT 0,
            note     TEXT,
            updated  TEXT,
            PRIMARY KEY (market, code)
        );

        CREATE TABLE IF NOT EXISTS daily_bars (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            market  TEXT NOT NULL,
            code    TEXT NOT NULL,
            date    TEXT NOT NULL,             -- YYYY-MM-DD
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,
            amount  REAL,
            source  TEXT,
            UNIQUE (market, code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_code
            ON daily_bars(market, code, date);
        CREATE INDEX IF NOT EXISTS idx_daily_market_date
            ON daily_bars(market, date);

        CREATE TABLE IF NOT EXISTS min_bars (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            market     TEXT NOT NULL,
            code       TEXT NOT NULL,
            timeframe  TEXT NOT NULL,          -- '1m' / '5m' / ...
            ts         TEXT NOT NULL,          -- ISO8601 本地时间（含日期）
            open       REAL,
            high       REAL,
            low        REAL,
            close      REAL,
            volume     REAL,
            source     TEXT,
            UNIQUE (market, code, timeframe, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_min_code
            ON min_bars(market, code, timeframe, ts);

        CREATE TABLE IF NOT EXISTS ticks (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            market  TEXT NOT NULL,
            code    TEXT NOT NULL,
            ts_ms   INTEGER NOT NULL,
            price   REAL,
            qty     REAL,
            side    TEXT,
            source  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ticks_code
            ON ticks(market, code, ts_ms);

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT,
            ts    TEXT
        );
        """
    )


# -------- 元信息 --------
def upsert_stock(market: str, code: str, *, name: str | None = None,
                exchange: str | None = None, listed: bool = True,
                delisted: bool = False, note: str | None = None) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _cx().execute(
        """
        INSERT INTO stocks (market, code, name, exchange, listed, delisted, note, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market, code) DO UPDATE SET
            name=COALESCE(excluded.name, stocks.name),
            exchange=COALESCE(excluded.exchange, stocks.exchange),
            listed=excluded.listed,
            delisted=excluded.delisted,
            note=COALESCE(excluded.note, stocks.note),
            updated=excluded.updated
        """,
        (market, code, name, exchange, int(listed), int(delisted), note, ts),
    )


def get_stock(market: str, code: str) -> dict | None:
    row = _cx().execute(
        "SELECT * FROM stocks WHERE market=? AND code=?",
        (market, code),
    ).fetchone()
    return dict(row) if row else None


def list_universe(market: str | None = None, only_listed: bool = True) -> list[dict]:
    """返回 [{market, code, name, ...}, ...]；不查K线，速度极快。"""
    if market:
        sql = "SELECT * FROM stocks WHERE market=?"
        params: tuple = (market,)
        if only_listed:
            sql += " AND listed=1 AND delisted=0"
        sql += " ORDER BY code"
    else:
        sql = "SELECT * FROM stocks WHERE 1=1"
        params = ()
        if only_listed:
            sql += " AND listed=1 AND delisted=0"
        sql += " ORDER BY market, code"
    return [dict(r) for r in _cx().execute(sql, params).fetchall()]


# -------- K线 upsert / load --------
def upsert_daily_bars(market: str, code: str, rows: list[dict],
                      source: str = "") -> int:
    """rows = [{date, open, high, low, close, volume, amount?}, ...]；返回写入条数。"""
    if not rows:
        return 0
    conn = _cx()
    sql = (
        "INSERT INTO daily_bars (market, code, date, open, high, low, close, volume, amount, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(market, code, date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume, amount=COALESCE(excluded.amount, daily_bars.amount), "
        "source=excluded.source"
    )
    params = [
        (market, code, r["date"],
         r.get("open"), r.get("high"), r.get("low"),
         r.get("close"), r.get("volume"), r.get("amount"),
         source or r.get("source") or "")
        for r in rows
    ]
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, params)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(params)


def upsert_min_bars(market: str, code: str, timeframe: str,
                    rows: list[dict], source: str = "") -> int:
    """rows 中 ts 字段 = ISO 日期字符串（'YYYY-MM-DD' 或 'YYYY-MM-DDTHH:MM:SS+00:00'）。"""
    if not rows:
        return 0
    conn = _cx()
    sql = (
        "INSERT INTO min_bars (market, code, timeframe, ts, open, high, low, close, volume, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(market, code, timeframe, ts) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume, source=excluded.source"
    )
    params = [
        (market, code, timeframe, ts,
         r.get("open"), r.get("high"), r.get("low"),
         r.get("close"), r.get("volume"), source or r.get("source") or "")
        for r in rows
        if (ts := r.get("ts") or r.get("date"))
    ]
    if not params:
        return 0
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, params)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(params)


def insert_ticks(market: str, code: str, rows: list[dict],
                 source: str = "") -> int:
    """rows = [{ts_ms, price, qty?, side?}, ...]；纯追加，不去重。"""
    if not rows:
        return 0
    conn = _cx()
    sql = ("INSERT INTO ticks (market, code, ts_ms, price, qty, side, source) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    params = [
        (market, code, int(r["ts_ms"]), r.get("price"), r.get("qty"),
         r.get("side") or "", source or r.get("source") or "")
        for r in rows if r.get("ts_ms") is not None
    ]
    if not params:
        return 0
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, params)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(params)


def count_ticks(market: str | None = None, code: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM ticks WHERE 1=1"
    params: list = []
    if market:
        sql += " AND market=?"
        params.append(market)
    if code:
        sql += " AND code=?"
        params.append(code)
    row = _cx().execute(sql, params).fetchone()
    return row[0] if row else 0


def last_tick_ts(market: str, code: str) -> int | None:
    row = _cx().execute(
        "SELECT MAX(ts_ms) FROM ticks WHERE market=? AND code=?",
        (market, code),
    ).fetchone()
    return row[0] if row and row[0] else None


def load_daily_bars(market: str, code: str, *, limit: int | None = None,
                    start: str | None = None, end: str | None = None
                    ) -> dict | None:
    """返回 {market, code, dates, open, high, low, close, volume, amount}。"""
    sql = "SELECT date, open, high, low, close, volume, amount FROM daily_bars WHERE market=? AND code=?"
    params: list = [market, code]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date"
    rows = _cx().execute(sql, params).fetchall()
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    dates, o, h, l, c, v, amt = zip(*[(r[0], r[1], r[2], r[3], r[4],
                                      r[5] or 0.0, r[6] or 0.0) for r in rows])
    return {
        "market": market, "code": code,
        "dates": list(dates),
        "open": np.array(o, dtype=np.float64),
        "high": np.array(h, dtype=np.float64),
        "low": np.array(l, dtype=np.float64),
        "close": np.array(c, dtype=np.float64),
        "volume": np.array(v, dtype=np.float64),
        "amount": np.array(amt, dtype=np.float64),
    }


def load_min_bars(market: str, code: str, timeframe: str,
                  *, limit: int | None = None,
                  start: str | None = None, end: str | None = None) -> dict | None:
    sql = ("SELECT ts, open, high, low, close, volume FROM min_bars "
           "WHERE market=? AND code=? AND timeframe=?")
    params: list = [market, code, timeframe]
    if start:
        sql += " AND ts>=?"
        params.append(start)
    if end:
        sql += " AND ts<=?"
        params.append(end)
    sql += " ORDER BY ts"
    rows = _cx().execute(sql, params).fetchall()
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    ts, o, h, l, c, v = zip(*[(r[0], r[1], r[2], r[3], r[4],
                              r[5] or 0.0) for r in rows])
    return {
        "market": market, "code": code, "timeframe": timeframe,
        "ts": list(ts),
        "open": np.array(o, dtype=np.float64),
        "high": np.array(h, dtype=np.float64),
        "low": np.array(l, dtype=np.float64),
        "close": np.array(c, dtype=np.float64),
        "volume": np.array(v, dtype=np.float64),
    }


def last_date(market: str, code: str) -> str | None:
    row = _cx().execute(
        "SELECT MAX(date) FROM daily_bars WHERE market=? AND code=?",
        (market, code),
    ).fetchone()
    return row[0] if row and row[0] else None


def last_ts(market: str, code: str, timeframe: str) -> str | None:
    row = _cx().execute(
        "SELECT MAX(ts) FROM min_bars WHERE market=? AND code=? AND timeframe=?",
        (market, code, timeframe),
    ).fetchone()
    return row[0] if row and row[0] else None


def count_bars(market: str, code: str, *, daily: bool = True) -> int:
    tbl = "daily_bars" if daily else "min_bars"
    row = _cx().execute(
        f"SELECT COUNT(*) FROM {tbl} WHERE market=? AND code=?", (market, code)
    ).fetchone()
    return row[0] if row else 0


# -------- meta --------
def set_meta(key: str, value: str) -> None:
    _cx().execute(
        "INSERT INTO meta (key, value, ts) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
        (key, value, time.strftime("%Y-%m-%d %H:%M:%S")),
    )


def get_meta(key: str) -> str | None:
    row = _cx().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# -------- 健康度 --------
def health() -> dict:
    conn = _cx()
    out = {"ts": DateTime_now()}
    for mkt in ("CN", "HK", "US", "CRYPTO"):
        s = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE market=?", (mkt,)
        ).fetchone()[0]
        d = conn.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE market=?", (mkt,)
        ).fetchone()[0]
        out[mkt] = {"stocks": s, "daily_bars": d}
    mn = conn.execute("SELECT COUNT(*) FROM min_bars").fetchone()[0]
    out["min_bars"] = mn
    tk = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    out["ticks"] = tk
    return out


def DateTime_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    h = health()
    for k, v in h.items():
        print(k, v)