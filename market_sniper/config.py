# -*- coding: utf-8 -*-
"""全局设置：JSON 持久化（默认 data/settings.json）。

用法：
    cfg = get_config()
    cfg.get("network.proxy_port")          # 7890
    cfg.set("feed.symbols", ["BTC/USDT"])
    cfg.save()
    cfg.proxy_url()                        # 'http://127.0.0.1:7890' 或 None

环境变量 MARKET_SNIPER_SETTINGS 可覆盖配置文件路径。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading

log = logging.getLogger("market_sniper.config")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DEFAULT_PATH = os.path.join(_ROOT, "data", "settings.json")

DEFAULTS: dict = {
    "network": {
        "proxy_enabled": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": 7890,
        "timeout": 15,
    },
    "sources": {
        # 各市场历史/延时数据源（HK/US 免费源延迟约15分钟；CRYPTO 为实时交易所）
        "HK": "yfinance",
        "US": "yfinance",
        "CRYPTO": "binance",       # binance | okx | bybit | gate
    },
    "feed": {
        "backend": "binance",          # binance | mock
        "exchange": "binance",
        "market": "CRYPTO",
        "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "streams": {
            "agg_trade": True,         # 逐笔成交
            "kline_1s": True,          # 1s K线
            "book_ticker": True,       # 最优盘口
        },
        "ring_size": 5000,
        "persist_ticks": False,
        "flush_ms": 1000,
        "reconnect_max_sec": 30,
    },
    "api": {
        "port": 7132,                  # 本地 HTTP API（浏览器插件用）
    },
    "backfill": {
        "on_start": True,              # 启动时后台自动补齐新数据
        "days": 30,                    # 日K增量窗口（已有数据则自动接续）
        "timeframes": ["1m", "5m", "15m", "30m", "60m"],
    },
    "strategy": {
        "enabled": False,
        "params": {},                  # 留给算法自行解释
    },
    "ui": {
        "default_market": "CRYPTO",
        "default_symbol": "BTC/USDT",
        "show_markers": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict | None = None, path: str | None = None):
        self.path = path or os.environ.get("MARKET_SNIPER_SETTINGS", _DEFAULT_PATH)
        self._data = _deep_merge(DEFAULTS, data or {})
        self._lock = threading.Lock()

    # -------- 读写 --------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = path or os.environ.get("MARKET_SNIPER_SETTINGS", _DEFAULT_PATH)
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            log.warning("设置文件读取失败（用默认值）: %s: %s", p, e)
            data = {}
        return cls(data, p)

    def save(self) -> None:
        with self._lock:
            payload = copy.deepcopy(self._data)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def reset(self) -> None:
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)

    # -------- 点路径访问 --------
    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self._data
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value

    def update(self, data: dict) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, data)

    @property
    def data(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    # -------- 派生 --------
    def proxy_url(self) -> str | None:
        if not self.get("network.proxy_enabled", True):
            return None
        host = self.get("network.proxy_host", "127.0.0.1")
        port = self.get("network.proxy_port", 7890)
        if not host:
            return None
        return f"http://{host}:{port}"

    def feed_symbols(self) -> list[str]:
        raw = self.get("feed.symbols") or []
        out = []
        for s in raw:
            s = str(s).strip().upper()
            if s and s not in out:
                out.append(s)
        return out


_singleton: Config | None = None
_singleton_lock = threading.Lock()


def get_config() -> Config:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = Config.load()
    return _singleton


if __name__ == "__main__":
    c = get_config()
    print(json.dumps(c.data, ensure_ascii=False, indent=2))
    print("proxy:", c.proxy_url())
    print("symbols:", c.feed_symbols())
