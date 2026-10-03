# -*- coding: utf-8 -*-
"""计算端约定（compute/ 目录）

这个目录放**自包含的计算脚本**：不 import market_sniper，stdin 收 JSON、
stdout 回 JSON（日志走 stderr）。Linux 优先，脚本建议加 shebang + 可执行位。

主程序通过 market_sniper/compute.py 的 run(name, payload) 转发调用：
    回测与日常信号共用同一计算层——
    信号: market_sniper.compute.compute_signals(algo, bars, market)
    回测: market_sniper.compute.backtest(algo, bars, market, tf)

现有脚本：
    boll_atr.py   BOLL 回归 + 仓位状态机信号（与 signals.py 同逻辑的独立版）
    backtest.py   标准回测引擎（下一根开盘成交 + 手续费/滑点 + 绩效指标）

新增算法：写一个 <algo>.py，注册进 signals.SIGNAL_ALGOS（进程内快路径），
或只放这里（转发慢路径）——两边任选其一即可被 /api/signal 与 CLI 调用。
"""
