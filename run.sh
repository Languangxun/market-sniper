#!/bin/bash
# market-sniper GUI 启动脚本
# 用法：./run.sh [选项]
cd "$(dirname "$(readlink -f "$0")")"
exec python3 main.py "$@"