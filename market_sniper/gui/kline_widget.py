# -*- coding: utf-8 -*-
"""K线 + 指标 GUI 组件（PyQt6 + pyqtgraph）。

布局：
    [价格K线 + 均线 + BOLL]      70%
    [成交量]                     12%
    [副图（MACD / KDJ / RSI）]   18%

特性：
    · 十字光标 + 同步 x 轴
    · 蜡烛红涨绿跌（亚洲习惯）；颜色主题可改
    · 副图动态切换
    · 周期：1m/15m/1d
"""
from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

# -------- 主题（高对比：对齐 stock_predict THEMES["contrast"]，与 main_window 同步） --------
BG          = "#000000"
PANEL_BG    = "#0a0a0a"
GRID_C      = "#3a3a3a"
GUIDE_C     = "#6b6b6b"
TEXT_C      = "#ffffff"
AXIS_C      = "#ffffff"
UP_C        = "#ff2d2d"   # 红涨（亚洲习惯）
DOWN_C      = "#00e676"   # 绿跌
VOL_UP_C    = "#ff2d2d"
VOL_DOWN_C  = "#00e676"
MA_COLORS = {5: "#ffb000", 10: "#00d4ff", 20: "#ff7ae0",
              30: "#39ff88", 60: "#ffee00"}
BOLL_UP_C   = "#ffee00"
BOLL_MID_C  = "#ff7ae0"
BOLL_LO_C   = "#ffee00"
C_ORANGE    = "#ffb000"
C_BLUE      = "#00b7ff"
C_PURPLE    = "#ff7ae0"
MACD_DIF    = C_ORANGE
MACD_DEA    = C_BLUE
MACD_UP     = UP_C
MACD_DN     = DOWN_C
CROSS_C     = "#ffff00"
TS_PRICE_C  = "#ffffff"   # 分时价格线
TS_AVG_C    = "#ffee00"   # 分时均价线

# 预创建画笔/刷子（避免每根蜡烛重建 QColor/mkPen——重建循环的最大开销）
_QC_UP = QtGui.QColor(UP_C)
_QC_DN = QtGui.QColor(DOWN_C)
_PEN_UP = pg.mkPen(_QC_UP.darker(140), width=0.6)
_PEN_DN = pg.mkPen(_QC_DN.darker(140), width=0.6)
_BR_UP = pg.mkBrush(_QC_UP)
_BR_DN = pg.mkBrush(_QC_DN)
_QV_UP = QtGui.QColor(VOL_UP_C); _QV_UP.setAlpha(160)
_QV_DN = QtGui.QColor(VOL_DOWN_C); _QV_DN.setAlpha(160)
_BR_VOL_UP = pg.mkBrush(_QV_UP)
_BR_VOL_DN = pg.mkBrush(_QV_DN)


# 自定义时间轴（轴刻度格式化）
class DateAxis(pg.AxisItem):
    """x 轴按数据 index 显示成 YYYY-MM-DD 或 MM-DD HH:MM。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._labels: list[str] = []

    def set_labels(self, labels: list[str]):
        self._labels = labels

    def tickStrings(self, values, scale, spacing):
        n = len(self._labels)
        out = []
        for v in values:
            iv = int(round(v))
            if 0 <= iv < n:
                out.append(self._labels[iv])
            else:
                out.append("")
        return out


# 百分比右轴（分时模式）：显示相对昨收的 %；candle 模式回退为价格
class PercentAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prev = 0.0

    def set_prev_close(self, pc: float):
        self._prev = float(pc or 0.0)

    def tickStrings(self, values, scale, spacing):
        if self._prev > 0:
            return [f"{(v - self._prev) / self._prev * 100:+.2f}%" for v in values]
        return [f"{v:.2f}" for v in values]


def _ts_label(s: str) -> str:
    """ISO UTC ts -> 本地时区 HH:MM（分时/分钟轴标签）。"""
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except ValueError:
        return str(s)[11:16] if len(str(s)) >= 16 else str(s)


# -------- 蜡烛 Item --------
class CandlestickItem(pg.GraphicsObject):
    """红涨绿跌蜡烛（亚洲习惯）。x 用整数 index。"""

    def __init__(self, opens, high, low, close):
        super().__init__()
        self.opens = np.asarray(opens, dtype=np.float64)
        self.high = np.asarray(high, dtype=np.float64)
        self.low = np.asarray(low, dtype=np.float64)
        self.close = np.asarray(close, dtype=np.float64)
        self._picture: Optional[QtGui.QPicture] = None
        self._cached_pixel_per_bar: float = -1.0
        self._pixel_per_bar: float = 6.0

    def set_pixel_per_bar(self, ppb: float):
        if abs(ppb - self._pixel_per_bar) > 0.5:
            self._pixel_per_bar = ppb
            self._picture = None
            self.update()

    def _rebuild_picture(self, pixel_per_bar: float):
        # body_w 是数据单位（x 是整数 index 0..n-1），蜡烛宽度 = 0.7 个 index 单位
        body_w = 0.7
        p = QtGui.QPicture()
        painter = QtGui.QPainter(p)
        try:
            # NaN 一次性向量化剔除；循环内走纯 float（避免 numpy 标量装箱）
            valid = ~(np.isnan(self.opens) | np.isnan(self.high) |
                      np.isnan(self.low) | np.isnan(self.close))
            idx = np.where(valid)[0].tolist()
            o_l = self.opens.tolist()
            h_l = self.high.tolist()
            l_l = self.low.tolist()
            c_l = self.close.tolist()
            drawLine = painter.drawLine
            drawRect = painter.drawRect
            setPen = painter.setPen
            setBrush = painter.setBrush
            QP = QtCore.QPointF
            QR = QtCore.QRectF
            half = body_w / 2.0
            for i in idx:
                o, h, l, c = o_l[i], h_l[i], l_l[i], c_l[i]
                if c >= o:
                    setPen(_PEN_UP); setBrush(_BR_UP)
                else:
                    setPen(_PEN_DN); setBrush(_BR_DN)
                x = float(i)
                drawLine(QP(x, l), QP(x, h))
                body_top = c if c >= o else o
                body_bot = o if c >= o else c
                drawRect(QR(x - half, body_bot,
                            body_w, max(body_top - body_bot, 1e-6)))
        finally:
            painter.end()
        self._cached_pixel_per_bar = pixel_per_bar
        return p

    def paint(self, painter, *_args):
        if (self._picture is None or
                abs(self._cached_pixel_per_bar - self._pixel_per_bar) > 0.5):
            self._picture = self._rebuild_picture(self._pixel_per_bar)
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if len(self.low) == 0:
            return QtCore.QRectF(0, 0, 1, 1)
        return QtCore.QRectF(-0.5, float(np.nanmin(self.low)),
                              len(self.close) + 1,
                              float(np.nanmax(self.high) - np.nanmin(self.low)))


# -------- 成交量 Bar --------
class VolumeItem(pg.GraphicsObject):
    def __init__(self, opens, closes, vols):
        super().__init__()
        self.opens = np.asarray(opens)
        self.closes = np.asarray(closes)
        self.vols = np.asarray(vols)
        self._picture: Optional[QtGui.QPicture] = None
        self._pixel_per_bar: float = 6.0
        self._cached_pixel_per_bar: float = -1.0

    def set_pixel_per_bar(self, ppb: float):
        if abs(ppb - self._pixel_per_bar) > 0.5:
            self._pixel_per_bar = ppb
            self._picture = None
            self.update()

    def _rebuild(self, pixel_per_bar: float):
        # 直接用真实成交量值画图：bar 从 y=0 长到 y=v
        bar_w = 0.7
        p = QtGui.QPicture()
        painter = QtGui.QPainter(p)
        try:
            vols = self.vols
            if not len(vols):
                self._cached_pixel_per_bar = pixel_per_bar
                return p
            valid = ~np.isnan(vols)
            idx = np.where(valid)[0].tolist()
            o_l = self.opens.tolist()
            c_l = self.closes.tolist()
            v_l = vols.tolist()
            no = len(o_l); nc = len(c_l)
            m = min(no, nc)
            setPen = painter.setPen
            setBrush = painter.setBrush
            drawRect = painter.drawRect
            QR = QtCore.QRectF
            setPen(QtCore.Qt.PenStyle.NoPen)
            half = bar_w / 2.0
            for i in idx:
                v = v_l[i]
                if i < m:
                    up = c_l[i] >= o_l[i]
                else:
                    up = False
                setBrush(_BR_VOL_UP if up else _BR_VOL_DN)
                x = float(i)
                drawRect(QR(x - half, 0.0, bar_w, v))
        finally:
            painter.end()
        self._cached_pixel_per_bar = pixel_per_bar
        return p

    def paint(self, painter, *_args):
        if (self._picture is None or
                abs(self._cached_pixel_per_bar - self._pixel_per_bar) > 0.5):
            self._picture = self._rebuild(self._pixel_per_bar)
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        vmax = float(np.nanmax(self.vols)) if len(self.vols) else 1.0
        return QtCore.QRectF(-0.5, 0, max(1, len(self.vols)) + 1, max(vmax, 1.0))


# -------- 副图绘制 --------
SUBPLOT_TYPES = ("MACD", "KDJ", "RSI", "ADX", "量比", "None")


@dataclass
class KlineData:
    market: str
    code: str
    timeframe: str           # '1d' / '1m' / '5m' / ...
    dates: list[str] = field(default_factory=list)
    opens: np.ndarray = field(default_factory=lambda: np.zeros(0))
    highs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    lows: np.ndarray = field(default_factory=lambda: np.zeros(0))
    closes: np.ndarray = field(default_factory=lambda: np.zeros(0))
    volumes: np.ndarray = field(default_factory=lambda: np.zeros(0))
    indicators: dict = field(default_factory=dict)
    name: str = ""
    mode: str = "candle"                     # 'candle' | 'timeshare'
    avg: Optional[np.ndarray] = None         # 分时均价线
    prev_close: float = 0.0                  # 分时基准（昨收）


class KlineWidget(QtWidgets.QWidget):
    """K 线 + 成交量 + 副图组件。"""

    subplot_changed = QtCore.pyqtSignal(str)   # 副图切换
    bar_hovered = QtCore.pyqtSignal(int)        # 鼠标 hover 索引

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=True, background=BG, foreground=TEXT_C)
        self.setStyleSheet(f"background:{BG};color:{TEXT_C};")

        self._data: Optional[KlineData] = None

        # ---- 主图布局 ----
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        gl = pg.GraphicsLayoutWidget()
        gl.setBackground(BG)
        outer.addWidget(gl, 1)

        self._glayout = gl  # 保存引用（set_fold 需要）

        # 价格轴：使用自定义 DateAxis 显示 YYYY-MM-DD / MM-DD HH:MM
        self._price_axis = DateAxis(orientation="bottom")
        self._price_axis.setPen(pg.mkPen(GRID_C))

        self._vol_axis = DateAxis(orientation="bottom")
        self._vol_axis.setPen(pg.mkPen(GRID_C))

        self._sub_axis = DateAxis(orientation="bottom")
        self._sub_axis.setPen(pg.mkPen(GRID_C))

        # 价格图（右轴为百分比轴，分时模式启用）
        self._pct_axis = PercentAxis(orientation="right")
        self._pct_axis.setPen(pg.mkPen(GRID_C))
        self._pct_axis.setTextPen(pg.mkPen(TEXT_C))
        self.p1 = gl.addPlot(row=0, col=0,
                             axisItems={"bottom": self._price_axis,
                                        "right": self._pct_axis})
        self.p1.setMenuEnabled(False)
        self.p1.showGrid(x=True, y=True, alpha=0.15)
        self.p1.getAxis("left").setPen(pg.mkPen(GRID_C))
        self.p1.getAxis("left").setTextPen(pg.mkPen(TEXT_C))
        self.p1.getAxis("right").setPen(pg.mkPen(GRID_C))
        self.p1.getAxis("right").setTextPen(pg.mkPen(TEXT_C))

        # 成交量图
        self.p2 = gl.addPlot(row=1, col=0, axisItems={"bottom": self._vol_axis})
        self.p2.setMenuEnabled(False)
        self.p2.showGrid(x=True, y=False, alpha=0.15)
        self.p2.getAxis("left").setPen(pg.mkPen(GRID_C))
        self.p2.getAxis("left").setTextPen(pg.mkPen(TEXT_C))
        self.p2.setMaximumHeight(120)
        self.p2.setXLink(self.p1)

        # 副图
        self.p3 = gl.addPlot(row=2, col=0, axisItems={"bottom": self._sub_axis})
        self.p3.setMenuEnabled(False)
        self.p3.showGrid(x=True, y=False, alpha=0.15)
        self.p3.getAxis("left").setPen(pg.mkPen(GRID_C))
        self.p3.getAxis("left").setTextPen(pg.mkPen(TEXT_C))
        self.p3.setMaximumHeight(140)
        self.p3.setXLink(self.p1)

        # 副图比例
        gl.ci.layout.setRowStretchFactor(0, 7)
        gl.ci.layout.setRowStretchFactor(1, 2)
        gl.ci.layout.setRowStretchFactor(2, 3)

        # 十字光标
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                       pen=pg.mkPen(CROSS_C, width=0.5,
                                                     style=QtCore.Qt.PenStyle.DashLine))
        self._hline = pg.InfiniteLine(angle=0, movable=False,
                                       pen=pg.mkPen(CROSS_C, width=0.5,
                                                     style=QtCore.Qt.PenStyle.DashLine))
        self.p1.addItem(self._vline, ignoreBounds=True)
        self.p1.addItem(self._hline, ignoreBounds=True)
        self.p1.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.p1.setMouseEnabled(x=True, y=True)
        # 监听 p1 宽度变化，更新每根 bar 像素（只连接一次）
        self.p1.getViewBox().sigResized.connect(self._apply_ppb)

        # 信息条
        self.info = QtWidgets.QLabel(self)
        self.info.setStyleSheet(f"color:{TEXT_C};padding:4px 6px;"
                                  f"font-family:Consolas,monospace;font-size:11px;")
        outer.addWidget(self.info)

        # 默认副图
        self._sub_type = "MACD"

        # 买卖点叠加（由算法/策略输出）
        self._markers: list[dict] = []
        self._marker_items: list = []

    # -------- 买卖点叠加 --------
    def set_markers(self, markers: list[dict] | None):
        """markers = [{index, price, side, hollow?}, ...]。
        side: 'up'(红,三角朝上) / 'down'(绿,三角朝下)；hollow=True 空心（平仓类）。"""
        self._markers = list(markers or [])
        self._draw_markers()

    def _draw_markers(self):
        for it in self._marker_items:
            try:
                self.p1.removeItem(it)
            except Exception:
                pass
        self._marker_items = []
        if not self._markers or not self._data:
            return
        n = len(self._data.closes)
        groups: dict[tuple[str, bool], list[tuple[int, float]]] = {}
        for m in self._markers:
            side = m.get("side") or "up"
            if 0 <= m.get("index", -1) < n:
                groups.setdefault((side, bool(m.get("hollow"))), []).append(
                    (m["index"], m["price"]))
        for (side, hollow), pts in groups.items():
            if not pts:
                continue
            x = [p[0] for p in pts]
            y = [p[1] for p in pts]
            color = UP_C if side == "up" else DOWN_C
            if hollow:
                it = pg.ScatterPlotItem(
                    x=x, y=y, symbol="t1" if side == "up" else "t",
                    size=12, pxMode=True,
                    brush=pg.mkBrush(BG), pen=pg.mkPen(color, width=1.4))
            else:
                it = pg.ScatterPlotItem(
                    x=x, y=y, symbol="t1" if side == "up" else "t",
                    size=12, pxMode=True,
                    brush=pg.mkBrush(color), pen=pg.mkPen("#ffffff", width=0.6))
            self.p1.addItem(it, ignoreBounds=True)
            self._marker_items.append(it)

    # -------- 数据注入 --------
    def set_data(self, data: KlineData):
        self._data = data
        # 清空旧
        self.p1.clear()
        self.p2.clear()
        self.p3.clear()
        self.p1.addItem(self._vline, ignoreBounds=True)
        self.p1.addItem(self._hline, ignoreBounds=True)

        if not data or len(data.closes) == 0:
            return

        n = len(data.closes)
        # x 轴标签：分时纯 HH:MM；分钟K 在日界标注日期（主流软件样式）
        if data.mode == "timeshare":
            labels = [_ts_label(d) for d in data.dates]
        elif data.timeframe == "1d":
            labels = [d[5:10] if len(d) >= 10 else d for d in data.dates]
        else:
            labels = []
            last_day = ""
            for d in data.dates:
                day = str(d)[:10]
                if day != last_day:
                    labels.append(f"{day[5:]} {_ts_label(d)}")
                    last_day = day
                else:
                    labels.append(_ts_label(d))
        self._price_axis.set_labels(labels)
        self._vol_axis.set_labels(labels)
        self._sub_axis.set_labels(labels)

        # 分时模式：价格线 + 均价线 + 昨收基准（独立绘制路径）
        if data.mode == "timeshare":
            self._draw_timeshare(data)
            return

        self._pct_axis.set_prev_close(0.0)
        self.set_fold(getattr(self, "_fold_sub", False))

        # K 线
        self._candle = CandlestickItem(data.opens, data.highs,
                                        data.lows, data.closes)
        self.p1.addItem(self._candle)
        self._vol_item = None

        # MA
        self._ma_lines = []
        ind = data.indicators
        for n_ in (5, 10, 20, 30, 60):
            arr = ind.get(f"ma{n_}")
            if arr is None or len(arr) != n:
                continue
            valid_x = np.where(~np.isnan(arr))[0]
            if len(valid_x) < 2:
                continue
            pen = pg.mkPen(MA_COLORS.get(n_, "#cccccc"), width=1.0)
            line = pg.PlotDataItem(valid_x.astype(float), arr[valid_x],
                                    pen=pen, antialias=False)
            self.p1.addItem(line)
            self._ma_lines.append((n_, line))

        # BOLL
        for col_name, color in (("boll_upper", BOLL_UP_C),
                                  ("boll_mid", BOLL_MID_C),
                                  ("boll_lower", BOLL_LO_C)):
            arr = ind.get(col_name)
            if arr is None or len(arr) != n:
                continue
            valid_x = np.where(~np.isnan(arr))[0]
            if len(valid_x) < 2:
                continue
            pen = pg.mkPen(color, width=0.8,
                           style=QtCore.Qt.PenStyle.DashLine)
            line = pg.PlotDataItem(valid_x.astype(float), arr[valid_x], pen=pen)
            self.p1.addItem(line)
            self._ma_lines.append((col_name, line))

        # 成交量
        self._vol = VolumeItem(data.opens, data.closes, data.volumes)
        self.p2.addItem(self._vol)
        self._vol_item = self._vol
        if ind.get("vol_ma5") is not None:
            arr = ind["vol_ma5"]
            valid_x = np.where(~np.isnan(arr))[0]
            if len(valid_x) >= 1:
                pen = pg.mkPen(C_ORANGE, width=1.0)
                self.p2.addItem(pg.PlotDataItem(valid_x.astype(float),
                                                 arr[valid_x], pen=pen))
        # 强制 Y 范围 [0, vmax] 防止 autoRange 收缩到 MA 区间
        vmax = float(np.nanmax(data.volumes)) if len(data.volumes) else 1.0
        self.p2.setYRange(0, vmax * 1.1, padding=0)
        self.p2.setXLink(self.p1)

        # 副图
        self._draw_subplot()

        # 视图范围（自动留点余白）
        lo = float(np.nanmin(data.lows))
        hi = float(np.nanmax(data.highs))
        pad = (hi - lo) * 0.05
        self.p1.setYRange(lo - pad, hi + pad, padding=0)
        self.p1.setXRange(0, n - 1, padding=0.01)

        # 买卖点
        self._marker_items = []
        self._draw_markers()

        # 主动触发一次 ppb 调整 + 强制重绘（确保 candle/vol paint 被调用）
        self._apply_ppb()
        for plot in (self.p1, self.p2, self.p3):
            plot.getViewBox().update()
            plot.replot()

        self._update_info_text()

    def _draw_timeshare(self, data: KlineData):
        """分时图：价格线 + 均价线 + 昨收虚线 + 涨跌着色成交量（主流软件样式）。"""
        n = len(data.closes)
        closes = data.closes
        self._pct_axis.set_prev_close(data.prev_close)
        self.p3.setVisible(False)
        layout = self._glayout.ci.layout
        layout.setRowStretchFactor(0, 7)
        layout.setRowStretchFactor(1, 3)
        layout.setRowStretchFactor(2, 0)
        layout.activate()
        self._candle = None
        self._vol_item = None
        self._ma_lines = []

        xv = np.arange(n)
        self._ts_price = pg.PlotDataItem(xv, closes,
                                          pen=pg.mkPen(TS_PRICE_C, width=1.2))
        self.p1.addItem(self._ts_price)
        if data.avg is not None and len(data.avg) == n:
            self._ts_avg = pg.PlotDataItem(xv, data.avg,
                                            pen=pg.mkPen(TS_AVG_C, width=1.0))
            self.p1.addItem(self._ts_avg)
        if data.prev_close > 0:
            self.p1.addItem(pg.InfiniteLine(
                pos=data.prev_close, angle=0,
                pen=pg.mkPen(GUIDE_C, width=0.8,
                             style=QtCore.Qt.PenStyle.DashLine)))

        # 成交量（按分钟相对前分钟涨跌着色）
        from pyqtgraph import BarGraphItem
        vols = data.volumes
        base = data.prev_close if data.prev_close > 0 else (
            closes[0] if n else 0.0)
        prev = np.concatenate(([base], closes[:-1]))
        up_mask = closes >= prev
        valid = ~np.isnan(vols) & (vols > 0)
        if valid.any():
            xv_b = xv[valid].astype(float)
            vb = vols[valid]
            um = up_mask[valid]
            if um.any():
                self.p2.addItem(BarGraphItem(x=xv_b[um], height=vb[um],
                                             width=0.7, brush=pg.mkBrush(VOL_UP_C)))
            if (~um).any():
                self.p2.addItem(BarGraphItem(x=xv_b[~um], height=vb[~um],
                                             width=0.7, brush=pg.mkBrush(VOL_DOWN_C)))
        vmax = float(np.nanmax(vols)) if len(vols) else 1.0
        self.p2.setYRange(0, vmax * 1.1, padding=0)
        self.p2.setXLink(self.p1)

        # y 范围：价格 + 均价 + 昨收都可见
        chunks = [closes[~np.isnan(closes)]]
        if data.avg is not None and len(data.avg) == n:
            a = data.avg[~np.isnan(data.avg)]
            if len(a):
                chunks.append(a)
        if data.prev_close > 0:
            chunks.append(np.array([data.prev_close]))
        allv = np.concatenate(chunks) if chunks else np.array([0.0, 1.0])
        lo, hi = float(np.min(allv)), float(np.max(allv))
        pad = (hi - lo) * 0.05 or (hi * 0.01 or 1.0)
        self.p1.setYRange(lo - pad, hi + pad, padding=0)
        self.p1.setXRange(0, max(n - 1, 1), padding=0.01)

        self._draw_markers()
        for plot in (self.p1, self.p2):
            plot.getViewBox().update()
            plot.replot()
        self._update_info_text()

    def showEvent(self, event):
        super().showEvent(event)
        # widget 首次显示后再触发一次 paint（避免 show 前 addItem 不被绘制）
        if self._data is not None:
            self._apply_ppb()
            self.p1.replot()
            self.p2.replot()

    def _apply_ppb(self):
        """根据 p1 视图宽度 + xRange 算每根 bar 占多少像素，通知 candle/vol 重画。"""
        if not self._data:
            return
        n = len(self._data.closes)
        if n <= 1:
            return
        vb = self.p1.getViewBox()
        view_w = vb.width() or 800
        x_range = vb.viewRect().width() or n
        ppb = max(1.0, view_w * (n / x_range) / n)
        if hasattr(self, "_candle") and self._candle is not None:
            self._candle.set_pixel_per_bar(ppb)
        if hasattr(self, "_vol_item") and self._vol_item is not None:
            self._vol_item.set_pixel_per_bar(ppb)

    def set_subplot(self, sub_type: str):
        if sub_type not in SUBPLOT_TYPES:
            return
        self._sub_type = sub_type
        self._draw_subplot()
        self.subplot_changed.emit(sub_type)

    def set_fold(self, folded: bool):
        """折叠副图：隐藏成交量 + 副图，腾出空间给主图。"""
        self.p2.setVisible(not folded)
        self.p3.setVisible(not folded)
        layout = self._glayout.ci.layout  # QGraphicsGridLayout
        if folded:
            layout.setRowStretchFactor(0, 1)
            layout.setRowStretchFactor(1, 0)
            layout.setRowStretchFactor(2, 0)
        else:
            layout.setRowStretchFactor(0, 7)
            layout.setRowStretchFactor(1, 2)
            layout.setRowStretchFactor(2, 3)
        layout.activate()

    def _draw_subplot(self):
        self.p3.clear()
        if not self._data:
            return
        data = self._data
        ind = data.indicators
        n = len(data.closes)

        if self._sub_type == "MACD":
            dif = ind.get("macd_dif")
            dea = ind.get("macd_dea")
            hist = ind.get("macd_hist")
            if dif is None:
                return
            xv = np.arange(n)
            self.p3.addItem(pg.PlotDataItem(xv, dif, pen=pg.mkPen(MACD_DIF, width=1)))
            self.p3.addItem(pg.PlotDataItem(xv, dea, pen=pg.mkPen(MACD_DEA, width=1)))
            # 柱：分两段（涨/跌）画
            from pyqtgraph import BarGraphItem
            valid = ~np.isnan(hist)
            if valid.any():
                xb = xv[valid].astype(float)
                hb = hist[valid]
                up_mask = hb >= 0
                if up_mask.any():
                    self.p3.addItem(BarGraphItem(x=xb[up_mask],
                                                    height=hb[up_mask],
                                                    width=0.6,
                                                    brush=pg.mkBrush(MACD_UP)))
                dn_mask = ~up_mask
                if dn_mask.any():
                    self.p3.addItem(BarGraphItem(x=xb[dn_mask],
                                                    height=hb[dn_mask],
                                                    width=0.6,
                                                    brush=pg.mkBrush(MACD_DN)))
        elif self._sub_type == "KDJ":
            kk = ind.get("kdj_k"); dd = ind.get("kdj_d"); jd = ind.get("kdj_j")
            if kk is None:
                return
            xv = np.arange(n)
            self.p3.addItem(pg.PlotDataItem(xv, kk, pen=pg.mkPen(C_ORANGE, width=1)))
            self.p3.addItem(pg.PlotDataItem(xv, dd, pen=pg.mkPen(C_BLUE, width=1)))
            self.p3.addItem(pg.PlotDataItem(xv, jd, pen=pg.mkPen(C_PURPLE, width=1)))
            self.p3.addItem(pg.InfiniteLine(pos=80, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))
            self.p3.addItem(pg.InfiniteLine(pos=20, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))
        elif self._sub_type == "RSI":
            for k, color in ((6, C_ORANGE), (14, C_BLUE), (24, C_PURPLE)):
                arr = ind.get(f"rsi_{k}")
                if arr is None:
                    continue
                valid_x = np.where(~np.isnan(arr))[0]
                if len(valid_x) < 2:
                    continue
                self.p3.addItem(pg.PlotDataItem(valid_x.astype(float),
                                                  arr[valid_x], pen=pg.mkPen(color, width=1)))
            self.p3.addItem(pg.InfiniteLine(pos=70, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))
            self.p3.addItem(pg.InfiniteLine(pos=30, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))
        elif self._sub_type == "ADX":
            for col, color in (("plus_di", UP_C),
                                  ("minus_di", DOWN_C),
                                  ("adx_14", C_ORANGE)):
                arr = ind.get(col)
                if arr is None:
                    continue
                valid_x = np.where(~np.isnan(arr))[0]
                if len(valid_x) < 2:
                    continue
                self.p3.addItem(pg.PlotDataItem(valid_x.astype(float),
                                                  arr[valid_x],
                                                  pen=pg.mkPen(color, width=1)))
            self.p3.addItem(pg.InfiniteLine(pos=25, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))
        elif self._sub_type == "量比":
            # 量比 = 当前成交量 / MA5 成交量
            v = ind.get("volume")
            vma5 = ind.get("vol_ma5")
            if v is None or vma5 is None:
                return
            from pyqtgraph import BarGraphItem
            ratio = v / np.where(vma5 == 0, np.nan, vma5)
            valid = ~np.isnan(ratio)
            if valid.any():
                xv = np.arange(n)[valid].astype(float)
                hv = ratio[valid]
                colors = ["#00e676" if x >= 1 else UP_C for x in hv]
                # BarGraphItem 不支持每根不同 brush，分两段
                up_mask = hv >= 1
                if up_mask.any():
                    self.p3.addItem(BarGraphItem(x=xv[up_mask], height=hv[up_mask],
                                                    width=0.7,
                                                    brush=pg.mkBrush(DOWN_C)))
                if (~up_mask).any():
                    self.p3.addItem(BarGraphItem(x=xv[~up_mask],
                                                    height=hv[~up_mask],
                                                    width=0.7,
                                                    brush=pg.mkBrush(UP_C)))
            self.p3.addItem(pg.InfiniteLine(pos=1.0, angle=0,
                                              pen=pg.mkPen(GRID_C, style=QtCore.Qt.PenStyle.DashLine)))

    # -------- 交互 --------
    def _on_mouse_moved(self, pos):
        if not self.p1.sceneBoundingRect().contains(pos):
            return
        x = self.p1.mapToView(pos).x()
        idx = int(round(x))
        if not self._data or idx < 0 or idx >= len(self._data.closes):
            return
        self._vline.setPos(idx)
        y = self.p1.mapToView(pos).y()
        self._hline.setPos(y)
        self.bar_hovered.emit(idx)
        self._update_info_text(idx)

    def _update_info_text(self, idx: int | None = None):
        if not self._data or len(self._data.closes) == 0:
            self.info.setText("")
            return
        d = self._data
        i = idx if idx is not None else len(d.closes) - 1
        i = max(0, min(i, len(d.closes) - 1))
        o, h, l, c, v = d.opens[i], d.highs[i], d.lows[i], d.closes[i], d.volumes[i]
        date = d.dates[i] if i < len(d.dates) else ""
        if not np.isnan(o) and o != 0:
            chg = (c - o) / o * 100
        else:
            chg = 0.0
        parts = [
            f"<b>{d.market}:{d.code}</b> {d.name or ''}  ·  {d.timeframe}",
            f"  {date}  O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}  "
            f"Δ={chg:+.2f}%  V={v:,.0f}",
        ]
        # 末尾指标
        ind = d.indicators
        tail = []
        for k in ("ma5", "ma10", "ma20", "ma60"):
            arr = ind.get(k)
            if arr is None:
                continue
            v_ = arr[i]
            if not np.isnan(v_):
                tail.append(f"{k.upper()}={v_:.2f}")
        if tail:
            parts.append("  " + " ".join(tail))
        self.info.setText("  ".join(parts))


if __name__ == "__main__":
    import sys
    from market_sniper.data import db
    from market_sniper import indicators as ind

    app = QtWidgets.QApplication(sys.argv)
    bars = db.load_daily_bars("CRYPTO", "BTC/USDT", limit=200)
    if not bars:
        print("no bars")
        sys.exit(0)
    i = ind.compute(bars)
    data = KlineData(
        market="CRYPTO", code="BTC/USDT", timeframe="1d",
        dates=list(bars["dates"]),
        opens=bars["open"], highs=bars["high"],
        lows=bars["low"], closes=bars["close"], volumes=bars["volume"],
        indicators=i, name="Bitcoin",
    )
    w = KlineWidget()
    w.set_data(data)
    w.resize(1100, 700)
    w.show()
    sys.exit(app.exec())