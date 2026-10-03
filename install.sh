#!/usr/bin/env bash
# market-sniper 客户端安装脚本（Linux）
# 用法：解压后进入目录执行 ./install.sh
set -e
cd "$(dirname "$(readlink -f "$0")")"

PY=python3
if ! $PY -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null; then
  echo "需要 Python 3.10+"; exit 1
fi

echo "[1/3] 创建虚拟环境并安装依赖…"
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

echo "[2/3] 初始化数据目录…"
mkdir -p data logs

echo "[3/3] 创建桌面快捷方式…"
DESK="$HOME/.local/share/applications/market-sniper.desktop"
mkdir -p "$(dirname "$DESK")"
cat > "$DESK" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=market-sniper
Name[zh]=market-sniper · 港美加密 K线研究
Comment=多市场 K线 + 指标 + 实时引擎 + 回测
Exec=$PWD/run.sh
Path=$PWD
Terminal=false
Categories=Office;Finance;
EOF
chmod +x run.sh

echo "完成！启动：./run.sh  （或应用列表里的 market-sniper）"
echo "首次启动会自动回填候选池数据，需配置代理（设置面板）。"
