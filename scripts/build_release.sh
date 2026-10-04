#!/usr/bin/env bash
# 打包 3 个 release 产物到 dist/：
#   market-sniper-client-vX-linux-x86_64.tar.gz   客户端（源码 + 安装脚本）
#   market-sniper-compute-vX-linux-x86_64.tar.gz  计算端（自包含脚本）
#   market-sniper-extension-vX.zip                浏览器插件
set -e
cd "$(dirname "$(readlink -f "$0")")/.."

VERSION=$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('market_sniper/__init__.py').read()).group(1))")
DIST=dist
rm -rf "$DIST"; mkdir -p "$DIST"

echo "==> 客户端"
STAGE=$(mktemp -d)/market-sniper-client-$VERSION
mkdir -p "$STAGE"
cp -r market_sniper compute scripts main.py run.sh install.sh \
      requirements.txt README.md LICENSE AGENTS.md "$STAGE/"
mkdir -p "$STAGE/data" "$STAGE/logs"
if [ -f data/models/hk/daily/index.json ] && [ -f data/models/hk/intraday/index.json ]; then
  echo "==> 注入 LightGBM 模型缓存"
  python3 scripts/pack_lgbm_cache.py --out "$STAGE/data/models_hk.tar.gz"
fi
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name "*.pyc" -delete
tar -C "$(dirname "$STAGE")" -czf \
    "$DIST/market-sniper-client-$VERSION-linux-x86_64.tar.gz" \
    "$(basename "$STAGE")"

echo "==> 计算端"
STAGE2=$(mktemp -d)/market-sniper-compute-$VERSION
mkdir -p "$STAGE2"
cp -r compute "$STAGE2/"
printf 'numpy>=1.24\n' > "$STAGE2/requirements.txt"
cat > "$STAGE2/README.md" <<EOF
# market-sniper 计算端 v$VERSION

自包含计算脚本：stdin 收 JSON、stdout 回 JSON（日志走 stderr）。
依赖：\`pip install -r requirements.txt\`（仅 numpy）。

## 信号
    echo '{"market":"US","tf":"1d","bars":{"dates":[...],"open":[...],"high":[...],"low":[...],"close":[...],"volume":[...]}}' \\
        | python3 compute/boll_atr.py

## 回测
    {"market","tf","bars","signals":[动作序列],"params":{fee_bps,slippage_bps,initial_cash,position_pct}}
        | python3 compute/backtest.py
        → {"metrics","trades","equity","params"}

与桌面客户端的 market_sniper/compute.py 转发层同协议，可单独部署到其他 Linux 机器。
EOF
tar -C "$(dirname "$STAGE2")" -czf \
    "$DIST/market-sniper-compute-$VERSION-linux-x86_64.tar.gz" \
    "$(basename "$STAGE2")"

echo "==> 浏览器插件"
STAGE3=$(mktemp -d)/market-sniper-extension-$VERSION
mkdir -p "$STAGE3"
cp -r extension/. "$STAGE3/"
(cd "$(dirname "$STAGE3")" && zip -qr "$OLDPWD/$DIST/market-sniper-extension-$VERSION.zip" \
    "$(basename "$STAGE3")")

echo "==> 产物："
ls -lh "$DIST"
