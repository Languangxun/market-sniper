# market-sniper 浏览器插件

连接本地 market-sniper 桌面程序，在主流行情网站的 K 线页面上叠加买卖点浮层。
**计算全部在本地程序完成**（示例算法 BOLL 回归 + ATR 止损参考，`market_sniper/signals.py`），
插件只做标的识别与展示，不发任何请求到外部服务器。

## 适配网站

| 网站          | 标的识别                                | 示例 URL                                        |
| ----------- | ----------------------------------- | --------------------------------------------- |
| TradingView | URL `?symbol=` 参数                   | `tradingview.com/chart/?symbol=NASDAQ%3AAAPL` |
| Binance     | `/trade/BTC_USDT`                   | `binance.com/zh-CN/trade/BTC_USDT`            |
| Yahoo 财经    | `/quote/AAPL`                       | `finance.yahoo.com/quote/0700.HK`             |
| 雪球          | `/S/00700`                          | `xueqiu.com/S/00700`                          |
| 长桥证券        | `/stock/00700-HK`、`/trade/HK.00700` | `longbridgeapp.com/stock/AAPL-US`             |

A股/新加坡（雪球 SH/SZ、长桥 -CN/-SG）不支持，会明确提示。

## 安装（Chrome / Edge）

1. 先启动 market-sniper 桌面程序（状态栏应显示 `API 127.0.0.1:7132`）
2. 打开 `chrome://extensions` → 右上角开启「开发者模式」
3. 「加载已解压的扩展程序」→ 选择本目录（`extension/`）
4. 打开上面任意网站的标的页面，右上角会出现 ⚡ 浮层：
   - `▲ BUY @价格` / `▼ SELL @价格` + 现价偏离 + 止损参考 + 最近信号列表
5. 点插件图标可开关显示、切信号周期（日K/60分/…/1分）、改服务地址

## 前提

- market-sniper GUI 必须在运行（API 随 GUI 启动，仅绑 127.0.0.1）
- 所查标的需已有本地数据（程序启动会自动增量补齐；也可手动回填）

## 换成你自己的算法

编辑 `market_sniper/signals.py`，按 `compute_boll_atr` 同签名写一个函数并
`SIGNAL_ALGOS["my_algo"] = my_algo`，插件请求带 `algo=my_algo` 即可（或改
`api.py` 的默认算法名）。
