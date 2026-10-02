# -*- coding: utf-8 -*-
"""market-sniper · 港美加密 + A股 短线狙击研究工具。

数据层四市场统一抽象：
    HK 港股   --  AAStocks（实时/历史日K）
    US 美股   --  yfinance（日K + 1分钟K）
    CRYPTO    --  ccxt (Binance 主，OKX 备)
    CN  A股   --  腾讯十源（沿用 stock_predict 思路，单源封装）

视图层：PyQt6 + pyqtgraph 蜡烛 + 副图。
"""
__version__ = "0.1.0"
MARKETS = ("CN", "HK", "US", "CRYPTO")