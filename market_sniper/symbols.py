# -*- coding: utf-8 -*-
"""三市场（HK / US / CRYPTO）代码本地/远程互转。

本地形态统一为：
    "<market>:<code>"
        HK     00700            (5 位，无前导零冗余前缀)
        US     AAPL             (大写字母+可选 ./- 类后缀原样保留)
        CRYPTO BTC/USDT         (BASE/QUOTE，大写)

市场代码通过 `to_local` / `to_remote` 双向互转；判定市场走 `infer_market`。
"""
from __future__ import annotations
import re


# ---------------- 市场识别 ----------------

# 港股代码：4~5 位数字；yfinance 形如 0700.HK
_HK_RE = re.compile(r"^\d{4,5}$")
# 美股：1~5 位字母、可含 . - ；yfinance 形如 AAPL / BRK.B
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
# 加密：BASE/QUOTE
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,15}$")

_PREFIXES = ("HK", "US", "CRYPTO")


def infer_market(sym: str) -> str:
    """从原始代码/带前缀字符串推断市场。无法判断时返回 'US' 兜底。

    特殊：'^HSI' 这种带 ^ 的指数代码归 US（yfinance 的指数都用 ^ 前缀）。
    """
    s = (sym or "").strip()
    if not s:
        return "US"
    if ":" in s:
        head, tail = s.split(":", 1)
        head = head.upper()
        if head in _PREFIXES or head == "CC":
            return "CRYPTO" if head == "CC" else head
        s = tail
    s_up = s.upper()
    s_lo = s.lower()
    s_no = s.replace(".", "").replace("-", "").replace("/", "")
    # yfinance 指数代码 '^HSI' / '^GSPC' → 走 yahoo
    if s.startswith("^"):
        return "US"
    # 港股带前缀（hk00700）
    if s_lo.startswith("hk") and s_lo[2:].isdigit():
        return "HK"
    # 港股：0700.HK / 00700 / 700
    if s_up.endswith(".HK"):
        return "HK"
    if _HK_RE.match(s_no):
        return "HK"
    # 加密：BTC/USDT
    if _CRYPTO_RE.match(s_up):
        return "CRYPTO"
    # 美股：纯字母（含 . -）
    if _US_RE.match(s_up) and not s_no.isdigit():
        return "US"
    return "US"


def to_local(sym: str) -> str:
    """原始代码 → 本地形态 <MKT>:<code>。"""
    s = (sym or "").strip()
    if not s:
        raise ValueError("empty symbol")
    if ":" in s and s.split(":", 1)[0].upper() in _PREFIXES:
        m, c = s.split(":", 1)
        return f"{m.upper()}:{_norm_code(c, m.upper())}"
    mkt = infer_market(s)
    return f"{mkt}:{_norm_code(s, mkt)}"


def _norm_code(code: str, mkt: str) -> str:
    code = code.strip()
    if mkt == "HK":
        c = code.upper().replace(".HK", "")
        if c.lower().startswith("hk"):
            c = c[2:]
        if c.isdigit():
            return c.zfill(5)
        return c
    if mkt == "US":
        return code.upper()
    if mkt == "CRYPTO":
        base, _, quote = code.partition("/")
        return f"{base.upper()}/{quote.upper()}"
    return code


def to_remote(sym: str, market: str | None = None) -> str:
    """本地形态 → 远程源形态（喂给 fetcher）。"""
    if ":" in sym and market is None:
        market, sym = sym.split(":", 1)
    market = (market or "US").upper()
    code = sym.strip()
    if market == "HK":
        c = code.upper().replace(".HK", "")
        if c.isdigit():
            c = c.zfill(5)
        return c  # AAStocks 直接吃 5 位 / yfinance 也吃 5 位
    if market == "US":
        return code.upper()
    if market == "CRYPTO":
        return code.upper()  # ccxt 直接吃 BASE/QUOTE
    return code


def yf_symbol(sym: str) -> str:
    """统一转 yfinance 形态（用于分钟K + 美股/港股备源）。"""
    if ":" in sym:
        market, code = sym.split(":", 1)
    else:
        market = infer_market(sym)
        code = sym
    market = market.upper()
    code = code.strip()
    if market == "HK":
        c = code.upper().replace(".HK", "")
        if c.isdigit():
            c = c.zfill(5).lstrip("0").zfill(4)  # 00700 -> 0700
        return f"{c}.HK"
    if market == "US":
        return code.upper()
    if market == "CRYPTO":
        # yfinance 支持 BTC-USD / ETH-USD；把 USDT 替成 USD
        base, _, quote = code.upper().partition("/")
        return f"{base}-USD"
    return code


def display_name(sym: str) -> str:
    """短显示形态（去前缀）。"""
    if ":" in sym:
        return sym.split(":", 1)[1]
    return sym


if __name__ == "__main__":
    for s in ["00700", "hk00700", "0700.HK", "AAPL", "BRK.B",
              "BTC/USDT", "tsla"]:
        print(f"{s:15s} -> market={infer_market(s):8s} local={to_local(s):15s} "
              f"remote={to_remote(to_local(s))} yf={yf_symbol(to_local(s))}")
