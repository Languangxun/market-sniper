# -*- coding: utf-8 -*-
"""四市场代码本地/远程互转。

本地形态统一为：
    "<market>:<code>"
        CN     sh600000 / sz000001 / bj920002
        HK     00700            (5 位，无前导零冗余前缀)
        US     AAPL             (大写字母+可选 ./- 类后缀原样保留)
        CRYPTO BTC/USDT         (BASE/QUOTE，BASE 大写 QUOTE 大写)

市场代码通过 `to_local` / `to_remote` 双向互转；判定市场走 `infer_market`。
"""
from __future__ import annotations
import re


# ---------------- 市场识别 ----------------

# 港股代码：5 位数字，首位 0~6 (含 8 为权证)；yfinance 形如 0700.HK
_HK_RE = re.compile(r"^(?:hk:)?0?[1-6]?\d{4}$|^[1-6]?\d{4}\.HK$", re.I)
# 美股：1~5 位字母、可含 . - ；yfinance 形如 AAPL / BRK.B
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
# 加密：BASE/QUOTE，全大写字母
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,15}$")
# A 股：sh/sz/bj + 6 位数字
_CN_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$", re.I)


def infer_market(sym: str) -> str:
    """从原始代码/带前缀字符串推断市场。无法判断时返回 'CN' 兜底。

    特殊：'^HSI' 这种带 ^ 的指数代码默认归 US（yfinance 的指数都用 ^ 前缀）。
    """
    s = (sym or "").strip()
    if not s:
        return "CN"
    if ":" in s:
        head, tail = s.split(":", 1)
        head = head.upper()
        if head in ("HK", "US", "CN", "CRYPTO", "CC"):
            return "CN" if head == "CC" else head
        s = tail
    s_up = s.upper()
    s_lo = s.lower()
    s_no = s.replace(".", "").replace("-", "").replace("/", "")
    # yfinance 指数代码 '^HSI' / '^GSPC' → 走 yahoo
    if s.startswith("^"):
        return "US"
    # 港股带前缀优先（避免与 sh900000 重判）
    if s_lo.startswith("hk") and s_lo[2:].isdigit():
        return "HK"
    # 港股：0700.HK / 00700 / 700
    if s_up.endswith(".HK"):
        return "HK"
    if s_no.isdigit() and len(s_no) in (4, 5):
        # 港股以 0~6 开头（部分含 8=牛熊证）；CN 6 位才有意义，故长度区分
        return "HK"
    # A 股：sh600000 / 600000 / sz000001 / 920002
    if _CN_RE.match(s_lo):
        return "CN"
    if s_no.isdigit() and len(s_no) == 6:
        return "CN"
    # 加密：BTC/USDT
    if _CRYPTO_RE.match(s_up):
        return "CRYPTO"
    # 美股：纯字母（含 . -）
    if _US_RE.match(s_up) and not s_no.isdigit():
        return "US"
    return "CN"


def to_local(sym: str) -> str:
    """原始代码 → 本地形态 <MKT>:<code>。"""
    s = (sym or "").strip()
    if not s:
        raise ValueError("empty symbol")
    if ":" in s and s.split(":", 1)[0].upper() in ("HK", "US", "CN", "CRYPTO"):
        m, c = s.split(":", 1)
        return f"{m.upper()}:{_norm_code(c, m.upper())}"
    mkt = infer_market(s)
    return f"{mkt}:{_norm_code(s, mkt)}"


def _norm_code(code: str, mkt: str) -> str:
    code = code.strip()
    if mkt == "CN":
        c = code.lower()
        if not (c.startswith(("sh", "sz", "bj")) and len(c) == 8):
            # 6 位数字补前缀
            if c.isdigit() and len(c) == 6:
                if c[0] in "69" or c[:2] in ("51", "56", "58"):
                    c = "sh" + c
                elif c[0] in "03" or c[:2] in ("15", "16", "18"):
                    c = "sz" + c
                elif c[0] in "48" or c[:2] in ("92", "43", "87"):
                    c = "bj" + c
                else:
                    c = "sh" + c
            else:
                return code
        return c
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
    market = (market or "CN").upper()
    code = sym.strip()
    if market == "CN":
        return code.lower()
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
        c = code.upper()
        if c.startswith("^"):
            return c
        return c
    if market == "CN":
        c = code.lower()
        if c.startswith(("sh", "sz", "bj")) and len(c) == 8:
            six = c[2:]
            exch = "SS" if c.startswith("sh") else ("SZ" if c.startswith("sz") else "BJ")
            return f"{six}.{exch}"
        return code
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
              "BTC/USDT", "sh600000", "600000", "tsla"]:
        print(f"{s:15s} -> market={infer_market(s):8s} local={to_local(s):15s} "
              f"remote={to_remote(to_local(s))} yf={yf_symbol(to_local(s))}")