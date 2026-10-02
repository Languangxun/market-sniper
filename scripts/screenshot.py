# -*- coding: utf-8 -*-
"""GUI 截图脚本（用于无头验证）。"""
import sys
from PyQt6 import QtCore, QtWidgets
from market_sniper import indicators as ind_mod
from market_sniper.data import db as dbmod
from market_sniper.gui.kline_widget import KlineWidget, KlineData


def widget_grab(widget, png: str):
    widget.repaint()
    pix = widget.grab()
    pix.save(png)
    print(f"saved {png} size={pix.width()}x{pix.height()}")


def shot_kline(png: str, market: str, code: str, timeframe: str = "1d",
                limit: int = 120):
    app = QtWidgets.QApplication(sys.argv)
    if timeframe == "1d":
        bars = dbmod.load_daily_bars(market, code, limit=limit)
    else:
        bars = dbmod.load_min_bars(market, code, timeframe, limit=limit)
    if not bars:
        print(f"no {market}:{code} {timeframe} bars")
        return
    ind = ind_mod.compute(bars)
    data = KlineData(
        market=market, code=code, timeframe=timeframe,
        dates=list(bars.get("dates") or bars.get("ts") or []),
        opens=bars["open"], highs=bars["high"],
        lows=bars["low"], closes=bars["close"], volumes=bars["volume"],
        indicators=ind, name="",
    )
    w = KlineWidget()
    w.resize(1280, 760)
    w.show()
    w.set_data(data)
    def take():
        widget_grab(w, png)
        app.quit()
    QtCore.QTimer.singleShot(1500, take)
    sys.exit(app.exec())


def shot_main(png: str):
    app = QtWidgets.QApplication(sys.argv)
    from market_sniper.gui.main_window import MainWindow
    win = MainWindow()
    win.resize(1400, 820)
    win.show()
    def take():
        widget_grab(win, png)
        app.quit()
    QtCore.QTimer.singleShot(2500, take)
    sys.exit(app.exec())


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("kline", "main", "min"), default="kline")
    p.add_argument("--out", default="/tmp/screenshot.png")
    p.add_argument("--market", default="CRYPTO")
    p.add_argument("--code", default="BTC/USDT")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--limit", type=int, default=120)
    a = p.parse_args()
    if a.mode == "main":
        shot_main(a.out)
    elif a.mode == "min":
        shot_kline(a.out, a.market, a.code, "1m", a.limit)
    else:
        shot_kline(a.out, a.market, a.code, a.timeframe, a.limit)