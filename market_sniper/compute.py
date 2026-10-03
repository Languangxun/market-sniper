# -*- coding: utf-8 -*-
"""计算转发层：把重计算转发给 compute/ 目录下的自包含脚本（Linux 优先）。

约定见 compute/README.md：脚本 stdin 收 JSON、stdout 回 JSON、不 import 主程序。
回测与日常信号共用这一层——GUI / API / CLI 都从这里走。

    run(name, payload)                        # 直接转发
    compute_signals(algo, bars, market)       # 进程内快路径优先，缺了再转发
    backtest(algo, bars, market, tf)          # 信号 + 回测一起转发计算

CLI：
    python -m market_sniper.compute list
    python -m market_sniper.compute signals --code HK:00700 --tf 1d
    python -m market_sniper.compute backtest --code US:AAPL --tf 1d --bars 800
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
COMPUTE_DIR = os.environ.get("MARKET_SNIPER_COMPUTE_DIR",
                             os.path.join(_ROOT, "compute"))


def list_scripts() -> list[str]:
    try:
        return sorted(f[:-3] for f in os.listdir(COMPUTE_DIR)
                      if f.endswith(".py") and not f.startswith("_"))
    except OSError:
        return []


def run(name: str, payload: dict, *, timeout: float = 120) -> dict:
    """转发给 compute/<name>.py：stdin JSON → stdout JSON。"""
    fname = name if name.endswith(".py") else f"{name}.py"
    path = os.path.join(COMPUTE_DIR, fname)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"计算脚本不存在: {path}（compute/ 现有: {', '.join(list_scripts())}）")
    if os.access(path, os.X_OK) and open(path, "rb").read(2) == b"#!":
        cmd = [path]                                  # 带执行位走 shebang
    else:
        cmd = [sys.executable, path]
    proc = subprocess.run(
        cmd, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"计算脚本 {name} 失败: "
                           f"{proc.stderr.decode('utf-8', 'replace')[-500:]}")
    out = proc.stdout.decode("utf-8", "replace")
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line:
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ValueError(f"计算脚本 {name} 无 JSON 输出")


def _bars_slim(bars: dict) -> dict:
    """db 输出（numpy 数组）→ 可 JSON 化的列表。"""
    out = {}
    for k, v in bars.items():
        if hasattr(v, "tolist"):
            out[k] = v.tolist()
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)
    return out


def compute_signals(algo: str, bars: dict, market: str,
                    params: dict | None = None) -> dict:
    """信号计算：先走进程内注册表（快），未注册再转发 compute/<algo>.py。"""
    from market_sniper import signals as sig_mod
    fn = sig_mod.SIGNAL_ALGOS.get(algo)
    if fn is not None:
        return fn(bars, market=market, **(params or {}))
    return run(algo, {"market": market, "bars": _bars_slim(bars),
                      "params": params or {}})


def backtest(algo: str, bars: dict, market: str, tf: str,
             params: dict | None = None,
             bt_params: dict | None = None) -> dict:
    """标准回测：信号 + 成交模拟都在计算端跑。"""
    sig = compute_signals(algo, bars, market, params)
    out = run("backtest", {
        "market": market, "tf": tf,
        "bars": _bars_slim(bars),
        "signals": sig.get("signals", []),
        "params": bt_params or {},
    })
    out.setdefault("algo", algo)
    out["position"] = sig.get("position")
    return out


# ---------------- CLI ----------------
def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from market_sniper.data import db
    from market_sniper.symbols import to_local

    p = argparse.ArgumentParser(prog="market_sniper.compute",
                                description="计算转发层 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出 compute/ 可用脚本")
    ps = sub.add_parser("signals", help="算信号")
    ps.add_argument("--code", required=True)
    ps.add_argument("--tf", default="1d")
    ps.add_argument("--algo", default="boll_atr")
    ps.add_argument("--bars", type=int, default=500)
    pb = sub.add_parser("backtest", help="标准回测")
    pb.add_argument("--code", required=True)
    pb.add_argument("--tf", default="1d")
    pb.add_argument("--algo", default="boll_atr")
    pb.add_argument("--bars", type=int, default=800)
    pb.add_argument("--fee-bps", type=float, default=5.0)
    pb.add_argument("--slippage-bps", type=float, default=2.0)
    pb.add_argument("--cash", type=float, default=1_000_000)
    args = p.parse_args(argv)

    if args.cmd == "list":
        for s in list_scripts():
            print(s)
        return 0

    local = to_local(args.code)
    market, _, code = local.partition(":")
    if args.tf == "1d":
        bars = db.load_daily_bars(market, code, limit=args.bars)
    else:
        bars = db.load_min_bars(market, code, args.tf, limit=args.bars)
    if not bars:
        print(f"本地无K线: {local} {args.tf}（先回填）", file=sys.stderr)
        return 1

    if args.cmd == "signals":
        out = compute_signals(args.algo, bars, market)
        for s in out.get("signals", [])[-10:]:
            print(f"{s['ts']}  {s['side']:12s} @ {s['price']}")
        print(f"仓位: {out.get('position')}  / {len(out.get('signals', []))} 个动作")
        return 0

    out = backtest(args.algo, bars, market, args.tf,
                   bt_params={"fee_bps": args.fee_bps,
                              "slippage_bps": args.slippage_bps,
                              "initial_cash": args.cash})
    m = out["metrics"]
    print(f"===== 回测 {local} {args.tf} · {args.algo} · 最近 {m['bars']} 根 =====")
    print(f"区间收益 {m['total_return_pct']:+.2f}%   年化 {m['annual_return_pct']:+.2f}%"
          f"   Sharpe {m['sharpe']:.2f}   最大回撤 {m['max_drawdown_pct']:.2f}%")
    print(f"交易 {m['n_trades']} 笔   胜率 {m['win_rate_pct']:.1f}%"
          f"   盈亏比 {m['profit_factor']}   平均持有 {m['avg_hold_bars']} 根"
          f"   仓位暴露 {m['exposure_pct']:.1f}%")
    print(f"净值 {m['initial_cash']:,.0f} → {m['final_equity']:,.0f}")
    for t in out["trades"][-8:]:
        print(f"  {t['side']:5s} {t['entry_ts']} {t['entry_price']} → "
              f"{t['exit_ts']} {t['exit_price']}  {t['return_pct']:+.2f}%"
              f"  PnL {t['pnl']:+,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
