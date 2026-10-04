# market-sniper

> 港 / 美 / 加密 **超短线研究 + 实时交易框架**：多市场 K 线、分时图、实时引擎、
> 仓位语义信号、港股 LightGBM 买卖点、标准回测、本地 API + 浏览器插件。
> UI 风格参考 stock_predict（高对比暗色主题）。

![主窗口](docs/screenshot_main.png)

## 三件套架构

```
┌────────────────────┐   WS 实时行情    ┌──────────────┐
│  客户端 (PyQt6)     │ ←────────────── │  Binance     │
│  · K线/分时/指标    │                 └──────────────┘
│  · 实时引擎+策略    │── 计算转发 ───→ ┌──────────────┐
│  · 本地 HTTP API    │                │  compute/    │  ← 可单独部署
│    127.0.0.1:7132  │←── JSON ──────  │  (计算端)    │
└─────────┬──────────┘                 └──────────────┘
          │ 买卖点
┌─────────▼──────────┐
│  浏览器插件 (MV3)   │  TradingView / Binance / Yahoo / 雪球 / 长桥
└────────────────────┘
```

- **客户端**：PyQt6 桌面程序。数据层（SQLite）、实时引擎、GUI、API、计算转发都在这里。
- **计算端**：`compute/` 下的自包含脚本（仅依赖 numpy），stdin/stdout JSON，
  可脱离主程序单独跑、单独部署；信号与回测共用同一转发协议。
- **插件**：只做标的识别 + 信号展示，计算全在本地客户端，不向任何外部服务器发数据。

## 功能

- **三市场统一**：HK 港股 / US 美股 / CRYPTO 加密，代码形态 `HK:00700` / `US:AAPL` / `CRYPTO:BTC/USDT`
- **周期**：分时（价格线+均价线+昨收基准+右轴百分比）/ 1m / 5m / 15m / 30m / 60m / 日K
- **指标**（numpy 向量化）：MA / EMA / BOLL / MACD / KDJ / RSI / ATR / DMI / ADX / OBV / CCI / 量比
- **实时引擎**：Binance WebSocket 组合流（逐笔 aggTrade + 1s K线 + 盘口 bookTicker），
  断线指数退避重连；事件总线 → 策略 → 信号 → K线买卖点叠加；离线 Mock 源联调
- **信号是仓位动作**（非独立 BS 点）：美股/加密 `开多 / 平多 / 开空 / 平空`，HK `买 / 卖`（只多）
- **港股 LightGBM 买卖点**：行业分组训练（日线未来 5 日 / 分时 5m 未来 12 根两套），
  验证集分位数自适应入场/出场阈值；模型缓存随发布包注入，首次使用自动解包；
  开发端数据领先 ≥N 天时后台增量重训，前台无感
- **标准回测**：下一根开盘成交 + 手续费/滑点 + 全仓，HK 只多 / US+CRYPTO 可空；
  收益/年化/Sharpe/回撤/胜率/盈亏比
- **批量回测**：`scripts/backtest_lgbm.py` 港股全库样本外回测（默认从模型 `valid_from` 起），
  CSV/Excel 报告含买入持有对比
- **离线扩池**：Stooq 整包下载（自动过 JS PoW + OCR 验证码）→ 导入日K/5分K，
  候选池可扩到全市场
- **启动自动补齐**：每次打开程序后台增量拉新（日K接续 + 分钟K刷新），
  `backfill.max_symbols` 封顶防狂刷
- **数据源可配置**：HK/US 实时报价可选腾讯（快）/ Yahoo（延时）；加密可选
  Binance / OKX / Bybit / Gate；代理热生效

![分时图](docs/screenshot_timeshare.png)

## 快速开始

```bash
# 依赖：Python 3.10+（Linux 优先）
pip install -r requirements.txt      # 或用 release 包里的 install.sh

# 首次回填（三市场候选池，日K全历史 + 分钟K）
python main.py --backfill

# 启动 GUI（首次需在设置里确认代理，默认 127.0.0.1:7890）
./run.sh
```

也可以直接从 GitHub Release 下载现成包：

- `market-sniper-client-vX-linux-x86_64.tar.gz`：客户端源码（含 LightGBM 模型缓存），
  解压后 `./install.sh` 自动建 venv + 桌面快捷方式，`./run.sh` 启动
- `market-sniper-compute-vX-linux-x86_64.tar.gz`：自包含计算端（仅 numpy）
- `market-sniper-extension-vX.zip`：浏览器插件

打开程序后台会自动补齐新数据；「回填」对话框可按代码（分号分隔）+ 根数 + 周期定向拉取。

## 港股 LightGBM 买卖点

数据链路：

```
scripts/sync_hk_industry.py   东财 F10 → data/hk_industry.json（行业 + 中文名，顺带回填 stocks）
scripts/train_hk_lgbm.py      按行业分组训练（同行业成员 + 全市场兜底）
scripts/pack_lgbm_cache.py    data/models/hk → data/models_hk.tar.gz（发布注入）
market_sniper/lgbm_model.py   推理（算法注册名 lgbm），ensure_cache() 首次自动解包
scripts/backtest_lgbm.py      全库批量回测（默认样本外，CSV/XLSX）
```

- **特征 28 维**（`market_sniper/features.py`，训练/推理共用）：动量 / 均线偏离 / MACD /
  RSI / KDJ / BOLL / ATR / 量比 / 振幅 + 行业指数相对强弱（`peer_*` / `rs*`）。
- **标签**：未来 5 日（日线）/ 未来 12 根 5m（分时）收益为正。
- **推理**：按图表周期自动选 daily / intraday 模型；入场阈值取验证集高分位
  （不低于中位 +0.02），出场取中位；`index.json` 记录验证切点 `valid_from`。
- **自动重训**：客户端默认关闭（模型随包注入）；开发端在 `data/settings.json` 打开
  `lgbm.auto_retrain`，本地数据领先 `data_last` ≥ `lgbm.retrain_days`（默认 5 天）时
  后台重训，前台继续用旧模型（`index.json` 最后原子替换）。

```bash
python3 scripts/sync_hk_industry.py                 # 行业 + 中文名
python3 scripts/train_hk_lgbm.py                    # 日线 + 分时全量
python3 scripts/train_hk_lgbm.py --timeframe daily --only 软件服务
python3 scripts/backtest_lgbm.py --timeframe all --limit 50
```

设置面板「图表信号算法」选 `lgbm`（仅港股；模型缺失或本地数据不足时返回提示）；
`ui.show_markers` 控制图表买卖点显示。

## 实时引擎与策略接口

工具栏「实时」启动引擎（数据源在设置里选 Binance / Mock）。
策略继承 `market_sniper/engine/strategy.py` 的 `Strategy`，在 `on_tick / on_bar`
里返回 `Signal` 即触发买卖点：

```python
from market_sniper.engine import Strategy, Signal

class MyStrategy(Strategy):
    name = "my"

    def on_tick(self, tick):            # tick: 逐笔成交（aggTrade）
        ...
        return Signal(tick.market, tick.code, tick.ts_ms,
                      side="open_long", price=tick.price, strategy=self.name)
```

信号 side 语义（展示元数据见 `signals.ACTION_META`）：

| 市场 | 动作 | K线标记 |
|---|---|---|
| US / CRYPTO | `open_long` 开多 / `close_long` 平多 / `open_short` 开空 / `close_short` 平空 | 实心▲红 / 空心▽绿 / 实心▼绿 / 空心△红 |
| HK（只多） | `buy` 买 / `sell` 卖 | ▲红 / ▼绿 |

逐笔落盘走 `ticks` 表 + 批量 writer（feed 回调内禁止直接写 SQLite）。

## 信号算法与计算转发层

信号与回测共用 `market_sniper/compute.py` 这一层，两种接入方式二选一：

1. **进程内快路径**：在 `market_sniper/signals.py` 写同签名函数并注册
   `SIGNAL_ALGOS["my_algo"] = my_algo`
2. **转发慢路径**：把自包含脚本扔进 `compute/`（stdin JSON → stdout JSON），
   无需注册即可被 `/api/signal`、CLI、回测调用

```bash
python -m market_sniper.compute list                                  # 可用脚本
python -m market_sniper.compute signals --code HK:00700 --tf 1d       # 信号
python -m market_sniper.compute backtest --code US:AAPL --tf 1d       # 回测报告
```

内置算法：

- `boll_atr`：BOLL 下/上轨回归 + 仓位状态机 + ATR 止损参考（全市场）
- `lgbm`：港股 LightGBM 买卖点（日线/分时自动选模型，仅 HK）

## 标准回测

引擎在 `compute/backtest.py`（自包含）。模型约定：

- 信号在**下一根 K 线开盘价**成交（无未来函数），开盘价加滑点
- 手续费双边 `fee_bps`、滑点 `slippage_bps`、全仓 `position_pct`
- HK 只做多；US / CRYPTO 可做空；逐 bar 净值 + 强制收尾平仓

输出：区间/年化收益、Sharpe、最大回撤、胜率、盈亏比、平均持有、仓位暴露、交易明细、净值曲线。

批量回测（港股 LGBM）：

```bash
python3 scripts/backtest_lgbm.py                 # 日线全库（样本外）
python3 scripts/backtest_lgbm.py --timeframe intraday
python3 scripts/backtest_lgbm.py --full          # 全历史（含训练段）
python3 scripts/backtest_lgbm.py --symbols HK:00700,HK:03690 --out /tmp/bt.xlsx
```

默认剔除无行业档案 / 低流动性标的，报告落在 `data/reports/`。

## 本地 API（随 GUI 启动，仅绑 127.0.0.1）

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 存活检查 |
| `GET /api/symbols` | 三市场标的池 |
| `GET /api/signal?code=HK:00700&tf=1d&algo=boll_atr` | 信号（code 支持无前缀自动识别） |
| `GET /api/backtest?code=BTC/USDT&tf=60m&bars=1500` | 回测（走计算转发层） |

端口 `config.api.port`（默认 7132），设置面板可改、热重启。

## 浏览器插件（extension/，MV3）

开着客户端时，在行情网站的 K 线页面上叠加买卖点浮层
（动作 + 价格 + 现价偏离 + 止损参考 + 当前仓位 + 最近信号）：

| 网站 | 标的识别 |
|---|---|
| TradingView | URL `?symbol=` 参数 |
| Binance | `/trade/BTC_USDT` |
| Yahoo 财经 | `/quote/AAPL` |
| 雪球 | `/S/00700` |
| 长桥证券 | `/stock/00700-HK`、`/trade/HK.00700` |

安装：`chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 → 选 `extension/`。
A股/新加坡会明确提示不支持。

![实时买卖点](docs/screenshot_live.png)

## 数据层

- SQLite（WAL）：`stocks` / `daily_bars` / `min_bars` / `ticks` / `meta`，
  路径默认 `data/market_cache.db`（`MARKET_SNIPER_DB` 可覆盖）
- 设置持久化 `data/settings.json`（`market_sniper/config.py`，`get_config()` 单例）

```jsonc
{
  "network":  { "proxy_enabled": true, "proxy_host": "127.0.0.1", "proxy_port": 7890 },
  "sources":  { "HK": "tencent", "US": "tencent", "CRYPTO": "binance" },
  "feed":     { "backend": "binance", "symbols": ["BTC/USDT"], "persist_ticks": false },
  "backfill": { "on_start": true, "days": 30, "max_symbols": 200 },
  "api":      { "port": 7132 },
  "strategy": { "enabled": false, "params": {} },
  "lgbm":     { "auto_retrain": false, "retrain_days": 5 },
  "ui":       { "signal_algo": "boll_atr", "show_markers": true }
}
```

## 常用脚本

| 脚本 | 用途 |
|---|---|
| `main.py --health` / `--backfill` | DB 健康度 / 默认候选池回填 |
| `market_sniper/compute.py` | 计算转发层 CLI（`list` / `signals` / `backtest`） |
| `scripts/sync_hk_industry.py` | 港股行业分类 + 中文名（东财 F10） |
| `scripts/train_hk_lgbm.py` | 港股 LightGBM 训练（日线 / 分时） |
| `scripts/backtest_lgbm.py` | 港股全库批量回测（样本外，CSV/XLSX） |
| `scripts/pack_lgbm_cache.py` | 打包模型缓存（发布包注入） |
| `scripts/download_stooq.py` | 下载 Stooq 整包（自动 PoW + OCR 验证码） |
| `scripts/import_stooq.py` | 导入 Stooq 日K / 5分K，离线扩池 |
| `scripts/build_release.sh` | 打包客户端 / 计算端 / 插件三产物到 `dist/` |

## 数据源与已知限制

| 市场 | 实时报价 | 日K | 分钟K | 备注 |
|---|---|---|---|---|
| HK | 腾讯 `qt.gtimg.cn`（快，默认）/ Yahoo（延时约15分） | yfinance 全历史 | 1m 限 7d / 5m~30m 限 60d / 60m 限 730d | 历史固定走 Yahoo |
| US | 腾讯（快，默认）/ Yahoo（延时约15分） | yfinance 全历史 | 同上 | 历史固定走 Yahoo |
| CRYPTO | 交易所 WebSocket 实时 | ccxt 全历史（分页） | ccxt 各档 1000 根 | 实时免费无上限 |

- Yahoo `range=max` 会降采样，程序内已改走显式起止区间（勿直接调 Yahoo 原生参数）
- 实时 WS 需代理可达 Binance；无代理环境会被 reset
- Stooq 离线包为华沙本地时间，导入时转 UTC；默认跳过已有 K 线，避免与 Yahoo 复权价拼接跳空
- LightGBM 模型仅覆盖港股；客户端默认不自动全量训练（模型随包注入）
- 不支持 A股（CN 已移除，不要加回来）

## License

[MIT](LICENSE) © 2026 Languangxun

仅供研究学习，不构成投资建议。
