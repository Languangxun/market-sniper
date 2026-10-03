#!/bin/bash
# market-sniper GUI 启动脚本
# 用法：./run.sh [选项]（--backfill / --health 见 main.py）
cd "$(dirname "$(readlink -f "$0")")"

# 系统 python 依赖齐 → 直接用；否则回落 .venv（install.sh 创建）
if python3 -c "import PyQt6, pyqtgraph, ccxt, pandas" 2>/dev/null; then
  PY=python3
elif [ -x ".venv/bin/python" ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" main.py "$@"
