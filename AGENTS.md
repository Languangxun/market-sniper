# AGENTS.md

## 项目约定

- 数据层三市场统一以 `MARKET:CODE` 形态存：`HK:00700` / `US:AAPL` / `CRYPTO:BTC/USDT`。
  不支持 A股（CN 已移除，源码/文档不要加回来）。
- 日 K 走 `daily_bars(market, code, date, OHLCV, source)`；分钟 K 走 `min_bars(market, code, timeframe, ts, OHLCV, source)`。
- 新增数据源需：
  - 实现 `fetch_daily(code, ...)` 与 `fetch_minute(code, timeframe, ...)`，返回 `[{ts(ISO), ohlcv, ...}, ...]`
  - 在 `market_sniper.data.fetcher` 里加路由分支
  - 在 `market_sniper.symbols.to_remote()` / `yf_symbol()` 给出对应远程代码
- 新增指标在 `market_sniper.indicators.compute()` 内计算；GUI 客户端只读 dict，不重算。
- **GUI 配色统一在 `main_window.py` 顶部常量（`BG / DARK_BG / UP / DOWN / MA_COLORS` 等）**——完全照 stock_predict 风格；K线组件统一在 `kline_widget.py` 顶部常量（`UP_C / DOWN_C` 等）。
- **副图类型**：`MACD / KDJ / RSI / ADX / 量比 / None`；新增副图需同时在 KlineWidget `_draw_subplot()` 加分支。
- **周期**：`ts`(分时，内部走 1m；crypto 取最近 24h，港美取最后交易日) / `1m/5m/15m/30m/60m` / `1d`；分时渲染在 KlineWidget `_draw_timeshare()`，右轴 `PercentAxis` 显示相对昨收 %，分钟轴标签统一转本机时区（`_ts_label`）。
- **数据源可配置**：`config.sources.{HK,US,CRYPTO}`（设置面板可选）；HK/US 实时报价可选 `tencent`（`sources/tencent_hk_us.py`，qt.gtimg.cn，仅报价）或 `yfinance`，日K/分钟K 历史固定走 yfinance；CRYPTO 走 `ccxt_crypto.exchange_order()` 动态优先级，改完设置会 `reset_exchanges()/reset_session()` 热生效。
- **港股 LightGBM 买卖点**：行业映射 `scripts/sync_hk_industry.py`（东财 F10 → `data/hk_industry.json`，顺带回填中文名）；
  训练 `scripts/train_hk_lgbm.py`（同行业分组 + 全市场兜底，**日线未来 5 日 / 分时 5m 未来 12 根**两套，产物 `data/models/hk/{daily,intraday}/`）；
  推理 `market_sniper/lgbm_model.py`，算法注册为 `lgbm`（仅港股，按图表周期自动选模型；入场/出场阈值按验证集分位数自适应）；
  模型缓存 `scripts/pack_lgbm_cache.py` 打包 `data/models/hk` → `data/models_hk.tar.gz`，发布时由 `build_release.sh` 注入客户端，首次用时 `ensure_cache()` 自动解包；
  客户端默认**不**自动全量训练（`lgbm.auto_retrain=False`）；开发端在 `data/settings.json` 打开，增量领先 `data_last` ≥ `lgbm.retrain_days`（默认 5 天）时后台重训，前台无感继续用旧模型；
  特征 `market_sniper/features.py` 训练/推理共用。
- **LightGBM 回测**：`scripts/backtest_lgbm.py`（全库批量；默认**样本外**：从模型 `valid_from`（训练/验证切点）起回测，`--full` 才看全历史；HK 只多、下一根开盘成交、手续费+滑点；默认剔除无行业档案/低流动性标的，输出 `data/reports/lgbm_{daily,intraday}_*.csv` 或 `.xlsx`）。
- **离线扩池**：`scripts/download_stooq.py`（自动 PoW + OCR 验证码，下 `d_*_txt` 日K / `5_*_txt` 5分K 到 `data/raw/`）→ `scripts/import_stooq.py`（默认只导 stocks 目录、每标的最近 1000 根、跳过已有K线避免与 Yahoo 复权价拼接跳空；`--timeframe 5m` 导 5 分K，时间戳为华沙本地时间需转 UTC；`source='stooq'`）。全市场池下启动自动补齐受 `backfill.max_symbols` 封顶（默认 200）。
- **设置统一走 `market_sniper/config.py`**（`get_config()` 单例，落盘 `data/settings.json`）；GUI 面板在 `gui/settings_dialog.py`，新增可配置项需同时改 `config.DEFAULTS` 和面板 `_load()/_collect()`。
- **实时链路**：`data/stream.py`(Feed) → `engine/runner.py`(LiveEngine) → `engine/strategy.py`(Strategy 返回 `Signal`) → GUI `_sig_engine_signal` → `kline_widget.set_markers()`。feed 回调内禁止写 SQLite，tick 落盘走 `ticks` 表 + `LiveEngine` 批量 writer。
- **图表 BS 点**：图表加载后自动跑 `compute.compute_signals`（算法 `ui.signal_algo`，
  设置面板可选；`ui.show_markers` 控制显示），与引擎实时信号在 `_apply_markers` 合并去重后画 marker。
- 代理不再硬编码：`network.*` 由 config 读取，`ccxt_crypto.reset_exchanges()` / `yfinance_us.reset_session()` 可在设置保存后热重置。
- **本地 API**：`market_sniper/api.py` 随 GUI 启动（端口 `config.api.port`，默认 7132，仅绑 127.0.0.1）；
  `/api/signal` 的算法在 `signals.SIGNAL_ALGOS` 注册（同签名函数），默认 `boll_atr` 示例；
  `/api/backtest` 走计算转发层。
- **计算转发层**：`market_sniper/compute.py` 把重计算转发给 `compute/` 目录的自包含脚本
  （stdin/stdout JSON、不 import 主程序、Linux 优先，可执行位走 shebang）。
  信号与回测共用这一层；CLI：`python -m market_sniper.compute {list|signals|backtest}`。
  新算法二选一：注册进 `SIGNAL_ALGOS`（进程内快路径）或只放 `compute/<algo>.py`（转发慢路径）。
- **浏览器插件**：`extension/`（MV3，TradingView/Binance/Yahoo/雪球/长桥），只做标的识别+展示，
  计算全在本地程序；标的映射规则在 `content.js toLocalCode()`。
- **回测模型约定**：信号下一根开盘成交、双边手续费 fee_bps + 滑点 slippage_bps、
  全仓（position_pct）、HK 只多 / US+CRYPTO 可空；引擎在 `compute/backtest.py`。
- **启动自动补齐**：`config.backfill.on_start` 控制开启，增量（日K接续 + 分钟K刷最近窗口），在 GUI 线程外跑。

## 提交前检查

```bash
# 健康度
python main.py --health

# 截图验证（无 GUI 环境需要 offscreen）
QT_QPA_PLATFORM=offscreen python scripts/screenshot.py --mode main --out /tmp/main.png
QT_QPA_PLATFORM=offscreen python scripts/screenshot.py --mode min --market CRYPTO --code BTC/USDT --out /tmp/min.png

# 数据层冒烟（连续跑三市场 5 标的）
python -m market_sniper.data.backfill --symbols HK:00700,US:AAPL,CRYPTO:BTC/USDT --days 30

# 启动实测（依赖装系统 python 后）
/home/lan/桌面/market-sniper/run.sh
```

## 已知陷阱

- yfinance.Ticker 对部分 HK 标的（00700 等）会触发 `quoteSummary` 404，因此 `yfinance_us.py`
  直接走 v8 chart endpoint（HTTP）。调用时只用 `fetch_daily / fetch_minute / fetch_quote`，
  不要用 Ticker.history()。
- pyqtgraph 自定义 `QGraphicsObject` 必须 **widget 已 show 之后** 才 paint 会被触发；
  KlineWidget 在 `showEvent` 主动调用 `_apply_ppb()` + `replot()` 强制首次绘制。
- ccxt crypto 必须配置代理（默认 `127.0.0.1:7890`，在设置面板/`data/settings.json` 改），
  无代理环境直接连接 Binance API 会被 reset。
- BinanceFeed 用 `websockets.sync.client`（在 `_run()` 内延迟导入），`websockets>=14` 必须装；
  断线会指数退避重连，代理参数由 config 提供。
- 快捷方式 `~/桌面/market-sniper.desktop` 依赖系统 python 已 install `ccxt yfinance PyQt6 pyqtgraph numpy pandas requests`，否则双击启动会报 `ModuleNotFoundError`。