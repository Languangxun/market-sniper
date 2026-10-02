# -*- coding: utf-8 -*-
"""market-sniper · 港美加密 短线狙击研究工具。

数据层三市场统一抽象：
    HK 港股   --  yfinance（v8 chart endpoint）
    US 美股   --  yfinance（日K + 1分钟K）
    CRYPTO    --  ccxt (Binance 主，OKX 备)

视图层：PyQt6 + pyqtgraph 蜡烛 + 副图。
"""
__version__ = "0.1.0"
MARKETS = ("HK", "US", "CRYPTO")