# -*- coding: utf-8 -*-
"""market-sniper 主窗口（PyQt6 + pyqtgraph）。

参考 stock_predict/stock_gui.py 的整体风格（暗黑密集布局）：
    - 工具栏：代码输入 / 周期 / 副图 / BOLL / 各 MA checkbox / 折叠副图
    - 信息行：标的简介 + 加载进度 + 悬停信息
    - 主体：自选池（左） / K 线三联图（中） / 实时报价 + 指标末值（右）
    - 五大市场指数条
    - 底部日志
"""
from __future__ import annotations

import datetime
import sys
import threading
import time
from typing import Optional

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from market_sniper import config as config_mod
from market_sniper import indicators as ind_mod
from market_sniper.data import db as dbmod
from market_sniper.data import fetcher
from market_sniper.gui.kline_widget import (
    KlineWidget, KlineData, SUBPLOT_TYPES,
)
from market_sniper.gui.settings_dialog import SettingsDialog

# ---------------- 主题色（高对比：对齐 stock_predict THEMES["contrast"]） ----------------
BG          = "#000000"
DARK_BG     = "#000000"
PANEL_BG    = "#0a0a0a"
FIELD_BG    = "#111111"
GRID_C      = "#3a3a3a"
GUIDE_C     = "#6b6b6b"
AXIS_C      = "#ffffff"
TITLE_TXT   = "#ffffff"
FG_MAIN     = "#ffffff"
BTN_BG      = "#000000"
BTN_FG      = "#ffffff"
BTN_HOVER   = "#2a2a2a"
BTN_BORDER  = "#ffffff"
BORDER      = "#ffffff"
SEL_BG      = "#555500"
HOVER_BG    = "#2a2a2a"
ACCENT      = "#ffee00"
LOG_BG      = "#000000"
UP          = "#ff2d2d"
DOWN        = "#00e676"
CROSS_C     = "#ffff00"
MACD_DIF    = "#ffb000"
MACD_DEA    = "#00b7ff"
MA_COLORS   = {5: "#ffb000", 10: "#00d4ff", 20: "#ff7ae0",
                30: "#39ff88", 60: "#ffee00"}
BOLL_UP     = "#ffee00"
BOLL_MID    = "#ff7ae0"
BOLL_LO     = "#ffee00"

# 5 个市场指数（用于顶部指数条）
# yfinance 指数代码统一 ^ 前缀（HSI/GSPC/DJI/IXIC）
INDEX_CODES = [
    ("US:^HSI",     "恒生指数"),
    ("US:^GSPC",    "标普500"),
    ("US:^DJI",     "道琼斯"),
    ("US:^IXIC",    "纳斯达克"),
    ("CRYPTO:BTC/USDT", "BTC"),
]


# ---------------- Worker（后台拉数据） ----------------
class FetchWorker(QtCore.QObject):
    finished = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, local: str, timeframe: str, parent=None):
        super().__init__(parent)
        self.local = local
        self.timeframe = timeframe

    @QtCore.pyqtSlot()
    def run(self):
        try:
            if self.timeframe == "1d":
                market, code = self.local.split(":", 1)
                last = dbmod.last_date(market, code)
                today = datetime.date.today()
                if last:
                    start = (datetime.datetime.strptime(last, "%Y-%m-%d") +
                              datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    days = max(5, (today - datetime.datetime.strptime(start, "%Y-%m-%d").date()).days + 3)
                else:
                    days = 365
                fetcher.backfill_daily(self.local, days=days)
            else:
                tf = "1m" if self.timeframe == "ts" else self.timeframe
                fetcher.backfill_minute(self.local, timeframe=tf, period="7d")
            self.finished.emit(self.local)
        except Exception as e:
            self.failed.emit(f"{self.local}: {e}")


# ---------------- 主窗口 ----------------
class MainWindow(QtWidgets.QMainWindow):
    MARKETS = ["HK", "US", "CRYPTO"]   # 默认隐藏 CN
    TIMEFRAMES = [("ts", "分时"), ("1m", "1分"), ("5m", "5分"),
                  ("15m", "15分"), ("30m", "30分"), ("60m", "60分"),
                  ("1d", "日K")]
    DEFAULT_SHOW_N = 120
    SUBPLOTS = ("MACD", "KDJ", "RSI", "ADX", "量比")
    FOLD_LIMITS = 380   # 主图高度 < 此值时默认折叠副图

    # 后台线程 → 主线程 的信号桥（pyqt 信号 emit 线程安全，自动队列到主线程）
    _sig_log = QtCore.pyqtSignal(str)
    _sig_quote = QtCore.pyqtSignal(str, dict)          # local, quote
    _sig_indices = QtCore.pyqtSignal(dict)             # {local: quote}
    _sig_backfill_done = QtCore.pyqtSignal(str)        # 摘要
    _sig_engine_signal = QtCore.pyqtSignal(dict)       # 策略 Signal.as_dict()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("market-sniper · 港美加密多市场 K线研究")
        self.resize(1480, 880)
        self.setStyleSheet(self._qss())

        # 状态
        self._current_market = "HK"
        self._current_code: str = "00700"
        self._current_timeframe = "1d"
        self._show_n = self.DEFAULT_SHOW_N
        self._subplot = "MACD"
        self._show_boll = True
        self._ma_on = {n: True for n in MA_COLORS}
        self._fold_sub = False
        self._watchlist: list[tuple[str, str]] = []  # (local, name)
        self._indices_busy = False
        self._backfill_busy = False

        # 设置 / 实时引擎
        self.config = config_mod.get_config()
        self._engine = None
        self._engine_signals: list[dict] = []

        self._build_toolbar()
        self._build_info_row()
        self._build_body()
        self._build_indices_bar()
        self._build_logbar()
        self._build_statusbar()

        # 后台线程结果回主线程
        self._sig_log.connect(self._log)
        self._sig_quote.connect(self._apply_quote)
        self._sig_indices.connect(self._apply_indices)
        self._sig_backfill_done.connect(self._on_backfill_done)
        self._sig_engine_signal.connect(self._on_engine_signal)

        # 启动即自选池默认项
        self._ensure_universe(self._current_market)
        self._refresh_pool()
        self._refresh_watchlist()
        self._set_intraday_controls()
        self._after_symbol_changed()

    # ---------------- 主题 ----------------
    def _qss(self) -> str:
        return f"""
        QMainWindow, QWidget {{
            background:{BG}; color:{FG_MAIN};
            font-family: "Microsoft YaHei", "Consolas", monospace;
            font-size: 9pt;
        }}
        QToolBar {{
            background:{PANEL_BG}; border:0; spacing:2px; padding:4px;
        }}
        QToolBar QToolButton, QPushButton {{
            background:{BTN_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER}; border-radius:3px;
            padding:3px 9px; font-size: 9pt;
        }}
        QToolBar QToolButton:hover, QPushButton:hover {{ background:{BTN_HOVER}; }}
        QToolBar QToolButton:checked {{
            background:{SEL_BG}; color:{ACCENT}; border-color:{ACCENT};
        }}
        QLineEdit, QComboBox, QSpinBox {{
            background:{FIELD_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER}; border-radius:3px;
            padding:3px 6px; selection-background-color:{SEL_BG};
        }}
        QComboBox QAbstractItemView {{
            background:{FIELD_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER};
        }}
        QListWidget {{
            background:{PANEL_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER}; border-radius:3px;
            font-family: Consolas, monospace; font-size: 9pt; outline:0;
        }}
        QListWidget::item {{ padding:4px 6px; }}
        QListWidget::item:hover {{ background:{HOVER_BG}; }}
        QListWidget::item:selected {{ background:{SEL_BG}; color:{ACCENT}; }}
        QLabel {{ color:{FG_MAIN}; font-size: 9pt; }}
        QLabel#title {{ color:{TITLE_TXT}; font-weight:bold; }}
        QLabel#subtle {{ color:{AXIS_C}; }}
        QLabel#accent {{ color:{ACCENT}; font-weight:bold; }}
        QLabel#up {{ color:{UP}; }}
        QLabel#down {{ color:{DOWN}; }}
        QSplitter::handle {{ background:{GRID_C}; }}
        QStatusBar {{
            background:{PANEL_BG}; color:{AXIS_C};
            font-size: 8pt; padding:2px 6px;
        }}
        QFrame#panel {{
            background:{PANEL_BG}; border:1px solid {BORDER};
            border-radius:4px;
        }}
        QTextEdit, QPlainTextEdit {{
            background:{LOG_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER}; border-radius:3px;
            font-family: Consolas, monospace; font-size: 9pt;
        }}
        QCheckBox {{ color:{FG_MAIN}; spacing:4px; }}
        QCheckBox::indicator {{
            width:13px; height:13px;
            border:1px solid {BTN_BORDER}; border-radius:2px;
            background:{FIELD_BG};
        }}
        QCheckBox::indicator:checked {{
            background:{ACCENT}; border-color:{ACCENT};
        }}
        QDialog {{ background:{BG}; }}
        QTabWidget::pane {{
            border:1px solid {BORDER}; background:{PANEL_BG};
        }}
        QTabBar::tab {{
            background:{BTN_BG}; color:{FG_MAIN};
            border:1px solid {BTN_BORDER}; border-bottom:0;
            padding:4px 12px; margin-right:1px;
        }}
        QTabBar::tab:selected {{ background:{SEL_BG}; color:{ACCENT}; }}
        QGroupBox {{
            border:1px solid {BORDER}; border-radius:3px;
            margin-top:8px; padding:8px 6px 6px 6px; color:{TITLE_TXT};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; left:8px; padding:0 4px;
        }}
        """

    # ---------------- 工具栏 ----------------
    def _build_toolbar(self):
        tb = QtWidgets.QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)

        # 市场
        tb.addWidget(QtWidgets.QLabel(" 代码: "))
        self.code_edit = QtWidgets.QLineEdit()
        self.code_edit.setPlaceholderText("00700 / AAPL / BTC/USDT")
        self.code_edit.setMaximumWidth(180)
        self.code_edit.returnPressed.connect(self._on_search)
        tb.addWidget(self.code_edit)

        self.market_group = QtWidgets.QButtonGroup(self)
        for m in self.MARKETS:
            btn = QtWidgets.QToolButton()
            btn.setText(m)
            btn.setCheckable(True)
            btn.setChecked(m == self._current_market)
            tb.addWidget(btn)
            self.market_group.addButton(btn)
            btn.clicked.connect(lambda _=False, m=m: self._on_market_changed(m))
        tb.addSeparator()

        # 周期（分时 / 分钟K / 日K）
        tb.addWidget(QtWidgets.QLabel(" 周期: "))
        self.tf_combo = QtWidgets.QComboBox()
        for tf, label in self.TIMEFRAMES:
            self.tf_combo.addItem(label, tf)
        idx = self.tf_combo.findData(self._current_timeframe)
        self.tf_combo.setCurrentIndex(max(0, idx))
        self.tf_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        tb.addWidget(self.tf_combo)

        # 显示根数
        tb.addWidget(QtWidgets.QLabel(" 根数: "))
        self.show_n_combo = QtWidgets.QComboBox()
        self.show_n_combo.addItems([str(n) for n in (30, 60, 90, 120, 240)])
        self.show_n_combo.setCurrentText(str(self._show_n))
        self.show_n_combo.setMaximumWidth(70)
        self.show_n_combo.currentTextChanged.connect(
            lambda v: (self._set_show_n(int(v)), self._rerender()))
        tb.addWidget(self.show_n_combo)

        # 副图
        tb.addWidget(QtWidgets.QLabel(" 副图: "))
        self.sub_combo = QtWidgets.QComboBox()
        self.sub_combo.addItems(self.SUBPLOTS)
        self.sub_combo.setCurrentText(self._subplot)
        self.sub_combo.setMaximumWidth(70)
        self.sub_combo.currentTextChanged.connect(
            lambda v: (self._set_subplot(v), self.kline.set_subplot(v)))
        tb.addWidget(self.sub_combo)

        # BOLL / MA checkboxes
        self.boll_chk = QtWidgets.QCheckBox("BOLL")
        self.boll_chk.setChecked(True)
        self.boll_chk.toggled.connect(self._on_boll_toggle)
        tb.addWidget(self.boll_chk)

        self.ma_chks = {}
        for n in sorted(MA_COLORS):
            chk = QtWidgets.QCheckBox(f"MA{n}")
            chk.setChecked(True)
            chk.setStyleSheet(f"QCheckBox {{ color:{MA_COLORS[n]}; }}")
            chk.toggled.connect(lambda v, n=n: self._on_ma_toggle(n, v))
            tb.addWidget(chk)
            self.ma_chks[n] = chk

        # 折叠副图
        self.fold_btn = QtWidgets.QToolButton()
        self.fold_btn.setText("📐 折叠副图")
        self.fold_btn.setCheckable(True)
        self.fold_btn.toggled.connect(self._on_fold_toggle)
        tb.addWidget(self.fold_btn)
        tb.addSeparator()

        # 功能按钮
        self.btn_analyze = QtWidgets.QToolButton()
        self.btn_analyze.setText("🔍 分析")
        self.btn_analyze.clicked.connect(self._on_analyze)
        tb.addWidget(self.btn_analyze)

        self.btn_refresh = QtWidgets.QToolButton()
        self.btn_refresh.setText("🔄 刷新")
        self.btn_refresh.clicked.connect(self._after_symbol_changed)
        tb.addWidget(self.btn_refresh)

        self.btn_backfill = QtWidgets.QToolButton()
        self.btn_backfill.setText("📥 回填")
        self.btn_backfill.clicked.connect(self._on_backfill)
        tb.addWidget(self.btn_backfill)

        self.btn_tools = QtWidgets.QToolButton()
        self.btn_tools.setText("⚙ 设置")
        self.btn_tools.clicked.connect(self._open_settings)
        tb.addWidget(self.btn_tools)

        self.btn_live = QtWidgets.QToolButton()
        self.btn_live.setText("● 实时")
        self.btn_live.setCheckable(True)
        self.btn_live.setToolTip("启动/停止实时行情引擎（数据源见设置）")
        self.btn_live.toggled.connect(self._on_live_toggle)
        tb.addWidget(self.btn_live)

    # ---------------- 信息行（股票信息 / 进度 / hover） ----------------
    def _build_info_row(self):
        info = QtWidgets.QFrame()
        info.setMaximumHeight(26)
        info.setStyleSheet(f"background:{BG}; border:0;")
        layout = QtWidgets.QHBoxLayout(info)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)

        self.info_lbl = QtWidgets.QLabel("输入代码如 00700 / AAPL / BTC/USDT，回车")
        self.info_lbl.setObjectName("title")
        layout.addWidget(self.info_lbl, 1)

        self.progress_lbl = QtWidgets.QLabel("")
        self.progress_lbl.setObjectName("accent")
        layout.addWidget(self.progress_lbl, 1, QtCore.Qt.AlignmentFlag.AlignCenter)

        self.hover_lbl = QtWidgets.QLabel("")
        self.hover_lbl.setObjectName("subtle")
        self.hover_lbl.setMinimumWidth(280)
        layout.addWidget(self.hover_lbl, 0, QtCore.Qt.AlignmentFlag.AlignRight)

        # 嵌入到 central 顶部
        return info

    # ---------------- 主体 ----------------
    def _build_body(self):
        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.setCentralWidget(central)

        v.addWidget(self._build_info_row())

        body = QtWidgets.QFrame()
        body_layout = QtWidgets.QHBoxLayout(body)
        body_layout.setContentsMargins(6, 4, 6, 4)
        body_layout.setSpacing(4)
        v.addWidget(body, 1)

        # ---- 左：自选池 + 候选池（一体化）----
        left = QtWidgets.QFrame()
        left.setObjectName("panel")
        left.setMinimumWidth(200)
        left.setMaximumWidth(280)
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(6, 4, 6, 4)
        lv.setSpacing(4)

        title_row = QtWidgets.QHBoxLayout()
        self.title_lbl = QtWidgets.QLabel("HK 候选池")
        self.title_lbl.setObjectName("title")
        title_row.addWidget(self.title_lbl, 1)
        lv.addLayout(title_row)

        # 候选池 list
        self.pool_list = QtWidgets.QListWidget()
        self.pool_list.itemActivated.connect(self._on_pool_activated)
        self.pool_list.currentItemChanged.connect(self._on_pool_changed)
        lv.addWidget(self.pool_list, 3)

        # 自选
        watch_box = QtWidgets.QFrame()
        watch_box.setObjectName("panel")
        wv = QtWidgets.QVBoxLayout(watch_box)
        wv.setContentsMargins(4, 2, 4, 4)
        wv.setSpacing(2)
        w_label = QtWidgets.QLabel("自选池")
        w_label.setObjectName("subtle")
        wv.addWidget(w_label)
        self.watch_list = QtWidgets.QListWidget()
        self.watch_list.itemActivated.connect(self._on_watch_activated)
        wv.addWidget(self.watch_list, 2)
        wb = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("＋ 加自选")
        b1.clicked.connect(self._add_to_watch)
        b2 = QtWidgets.QPushButton("－ 删除")
        b2.clicked.connect(self._del_from_watch)
        wb.addWidget(b1)
        wb.addWidget(b2)
        wv.addLayout(wb)
        lv.addWidget(watch_box, 2)

        # 今日市场（行业概览式）
        today_box = QtWidgets.QFrame()
        today_box.setObjectName("panel")
        tv = QtWidgets.QVBoxLayout(today_box)
        tv.setContentsMargins(4, 2, 4, 4)
        tv.setSpacing(2)
        today_label = QtWidgets.QLabel("今日市场")
        today_label.setObjectName("subtle")
        tv.addWidget(today_label)
        self.market_txt = QtWidgets.QTextEdit()
        self.market_txt.setReadOnly(True)
        self.market_txt.setMaximumHeight(110)
        self.market_txt.setHtml(self._default_market_html())
        tv.addWidget(self.market_txt, 1)
        lv.addWidget(today_box, 1)

        body_layout.addWidget(left, 0)

        # ---- 中：K线三联图（grid 6:2:3）----
        mid = QtWidgets.QFrame()
        mid.setObjectName("panel")
        mid.setStyleSheet(f"QFrame#panel {{ background:{BG}; border:0; }}")
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)
        body_layout.addWidget(mid, 1)

        self.kline = KlineWidget()
        mv.addWidget(self.kline, 1)
        self._mid = mid
        self._mv = mv

        # ---- 右：报价 + 指标末值 ----
        right = QtWidgets.QFrame()
        right.setObjectName("panel")
        right.setMinimumWidth(240)
        right.setMaximumWidth(320)
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(6, 4, 6, 4)
        rv.setSpacing(4)

        rt_title = QtWidgets.QLabel(" 实时报价 ")
        rt_title.setObjectName("title")
        rv.addWidget(rt_title)

        self.quote_box = QtWidgets.QLabel("（无数据）")
        self.quote_box.setWordWrap(True)
        self.quote_box.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.quote_box.setStyleSheet(
            f"background:{FIELD_BG}; border:1px solid {BTN_BORDER};"
            f"border-radius:3px; padding:8px; font-family:Consolas,monospace;"
        )
        rv.addWidget(self.quote_box)

        ind_title = QtWidgets.QLabel(" 指标末值 ")
        ind_title.setObjectName("title")
        rv.addWidget(ind_title)

        self.ind_label = QtWidgets.QLabel("待加载…")
        self.ind_label.setWordWrap(True)
        self.ind_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.ind_label.setStyleSheet(
            f"background:{FIELD_BG}; border:1px solid {BTN_BORDER};"
            f"border-radius:3px; padding:8px; font-family:Consolas,monospace;"
            f"font-size:8pt;"
        )
        rv.addWidget(self.ind_label, 1)

        # 市场状态条
        self.phase_var = QtWidgets.QLabel("市场状态 ● -")
        self.phase_var.setObjectName("accent")
        self.phase_var.setStyleSheet(
            f"color:{ACCENT}; font-weight:bold; padding:2px 0;"
        )
        rv.addWidget(self.phase_var, 0)

        body_layout.addWidget(right, 0)

    # ---------------- 五大指数条 ----------------
    def _build_indices_bar(self):
        self.idx_bar = QtWidgets.QFrame()
        self.idx_bar.setObjectName("panel")
        self.idx_bar.setStyleSheet(
            f"QFrame#panel {{ background:{PANEL_BG}; border-top:1px solid {BORDER}; }}"
        )
        self.idx_bar.setMaximumHeight(56)
        h = QtWidgets.QHBoxLayout(self.idx_bar)
        h.setContentsMargins(6, 2, 6, 2)
        h.setSpacing(0)

        title = QtWidgets.QLabel(" 五大市场指数 ")
        title.setObjectName("title")
        h.addWidget(title)

        self.idx_labels: dict[str, tuple[QtWidgets.QLabel, QtWidgets.QLabel]] = {}
        for local, name in INDEX_CODES:
            cell = QtWidgets.QFrame()
            cl = QtWidgets.QHBoxLayout(cell)
            cl.setContentsMargins(8, 0, 8, 0)
            cl.setSpacing(8)
            n = QtWidgets.QLabel(name)
            n.setObjectName("subtle")
            v = QtWidgets.QLabel("-")
            v.setStyleSheet(f"color:{FG_MAIN}; font-weight:bold;")
            c = QtWidgets.QLabel("-")
            c.setObjectName("subtle")
            cl.addWidget(n)
            cl.addWidget(v)
            cl.addWidget(c)
            h.addWidget(cell, 1, QtCore.Qt.AlignmentFlag.AlignCenter)
            self.idx_labels[local] = (v, c)

        # 插在 central 主布局里（在底部 logbar 之上）
        # 实际我们把 idx_bar 加进 kline 后面、logbar 之前
        # 先记下来后面 add
        self._idx_bar = self.idx_bar

    # ---------------- 底部日志 ----------------
    def _build_logbar(self):
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setMinimumHeight(60)

    # ---------------- 状态栏 ----------------
    def _build_statusbar(self):
        self.sb = self.statusBar()
        self.sb_msg = QtWidgets.QLabel("就绪")
        self.sb.addPermanentWidget(self.sb_msg, 1)
        self.sb_feed = QtWidgets.QLabel("实时: ○ 停止")
        self.sb_feed.setStyleSheet(f"color:{AXIS_C};")
        self.sb.addPermanentWidget(self.sb_feed)
        self.sb_health = QtWidgets.QLabel("DB: -")
        self.sb.addPermanentWidget(self.sb_health)

        # 重新装配 central：toolbar / info / body / idx / log
        # 已经在 _build_body 时把 info 和 body 加入了 central
        # 这里再把 idx_bar 和 log 加到 central
        central = self.centralWidget()
        v = central.layout()
        v.addWidget(self._idx_bar)
        v.addWidget(self.log)

        # 健康度定时刷新
        self._h_timer = QtCore.QTimer(self)
        self._h_timer.setInterval(5000)
        self._h_timer.timeout.connect(self._refresh_health)
        self._h_timer.start()
        self._refresh_health()

        # 指数定时刷新
        self._idx_timer = QtCore.QTimer(self)
        self._idx_timer.setInterval(30000)
        self._idx_timer.timeout.connect(self._refresh_indices)
        self._idx_timer.start()
        self._refresh_indices()

        # 实时引擎状态
        self._feed_timer = QtCore.QTimer(self)
        self._feed_timer.setInterval(1000)
        self._feed_timer.timeout.connect(self._refresh_feed_status)
        self._feed_timer.start()

    def _refresh_health(self):
        try:
            h = dbmod.health()
            msg = (f"DB  HK {h['HK']['stocks']}/{h['HK']['daily_bars']}  "
                   f"US {h['US']['stocks']}/{h['US']['daily_bars']}  "
                   f"CRYPTO {h['CRYPTO']['stocks']}/{h['CRYPTO']['daily_bars']}  "
                   f"MIN {h['min_bars']}  · {h['ts']}")
            self.sb_health.setText(msg)
        except Exception as e:
            self.sb_health.setText(f"DB err: {e}")

    # ---------------- 行为 ----------------
    def _ensure_universe(self, market: str):
        if not dbmod.list_universe(market):
            fetcher.ensure_stocks(market)

    def _on_market_changed(self, market: str):
        if market == self._current_market:
            return
        self._current_market = market
        rows = fetcher.universes(market)
        if rows:
            self._current_code = rows[0][0]
        self._ensure_universe(market)
        self._refresh_pool()
        self._after_symbol_changed()

    def _refresh_pool(self):
        self.pool_list.clear()
        rows = fetcher.universes(self._current_market)
        for code, name in rows:
            text = f"{code}  {name}" if name else code
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, code)
            self.pool_list.addItem(item)
        self.title_lbl.setText(f"{self._current_market} 候选池  ({len(rows)})")

    def _refresh_watchlist(self):
        self.watch_list.clear()
        for local, name in self._watchlist:
            market, code = local.split(":", 1)
            text = f"{code}  {name}" if name else code
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, local)
            self.watch_list.addItem(item)

    def _on_pool_changed(self, item, _prev):
        if item:
            self._current_code = item.data(QtCore.Qt.ItemDataRole.UserRole)
            self.code_edit.setText(self._current_code)
            self._update_info()

    def _on_pool_activated(self, item):
        self._current_code = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.code_edit.setText(self._current_code)
        self._after_symbol_changed()

    def _on_watch_activated(self, item):
        local = item.data(QtCore.Qt.ItemDataRole.UserRole)
        market, code = local.split(":", 1)
        # 切换市场 + 选中
        for btn in self.market_group.buttons():
            btn.setChecked(btn.text() == market)
        self._current_market = market
        self._current_code = code
        self._ensure_universe(market)
        self._refresh_pool()
        for i in range(self.pool_list.count()):
            if self.pool_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == code:
                self.pool_list.setCurrentRow(i)
                break
        self.code_edit.setText(code)
        self._after_symbol_changed()

    def _on_search(self):
        text = self.code_edit.text().strip()
        if not text:
            return
        try:
            local = fetcher.to_local(text)
        except Exception:
            local = f"{self._current_market}:{text}"
        market, code = local.split(":", 1)
        for btn in self.market_group.buttons():
            btn.setChecked(btn.text() == market)
        self._current_market = market
        self._current_code = code
        dbmod.upsert_stock(market, code, name="")
        self._ensure_universe(market)
        self._refresh_pool()
        for i in range(self.pool_list.count()):
            if self.pool_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == code:
                self.pool_list.setCurrentRow(i)
                break
        self._log(f"搜索 {local}")
        self._after_symbol_changed()

    def _add_to_watch(self):
        local = f"{self._current_market}:{self._current_code}"
        stock = dbmod.get_stock(self._current_market, self._current_code)
        name = (stock or {}).get("name") or ""
        if not any(l == local for l, _ in self._watchlist):
            self._watchlist.append((local, name))
            self._refresh_watchlist()
            self._log(f"+ 自选 {local} {name}")

    def _del_from_watch(self):
        cur = self.watch_list.currentItem()
        if cur is None:
            return
        local = cur.data(QtCore.Qt.ItemDataRole.UserRole)
        self._watchlist = [(l, n) for l, n in self._watchlist if l != local]
        self._refresh_watchlist()
        self._log(f"- 自选 {local}")

    def _on_analyze(self):
        self._log("分析：综合指标 + 形态概览（v0.1 占位）")
        self._refresh_indices()  # 顺手刷一下指数

    def _on_backfill(self):
        if self._backfill_busy:
            self._log("回填进行中，请稍候")
            return
        market = self._current_market
        tf = self._current_timeframe
        if tf == "ts":
            tf = "1m"
        self._backfill_busy = True
        self.btn_backfill.setEnabled(False)

        def work():
            try:
                if tf == "1d":
                    syms = fetcher.universes(market)
                    self._sig_log.emit(f"开始回填 {market} {len(syms)} 标的 最近 120 天日K…")
                    n_ok = n_fail = 0
                    for code, _ in syms:
                        try:
                            fetcher.backfill_daily(f"{market}:{code}", days=120)
                            n_ok += 1
                        except Exception as e:
                            n_fail += 1
                            self._sig_log.emit(f"  {market}:{code} 失败: {e}")
                    summary = f"回填完成 {market}：{n_ok} 成功 / {n_fail} 失败"
                else:
                    self._sig_log.emit(f"回填 {market}:{self._current_code} {tf}…")
                    fetcher.backfill_minute(f"{market}:{self._current_code}",
                                            timeframe=tf, period="7d")
                    summary = f"回填完成 {market}:{self._current_code} {tf}"
            except Exception as e:
                summary = f"回填失败: {e}"
            self._sig_backfill_done.emit(summary)

        threading.Thread(target=work, daemon=True, name="backfill").start()

    def _on_backfill_done(self, summary: str):
        self._backfill_busy = False
        self.btn_backfill.setEnabled(True)
        self._refresh_health()
        self._after_symbol_changed()
        self._log(summary)

    # ---------------- 设置 / 实时引擎 ----------------
    def _open_settings(self):
        dlg = SettingsDialog(self.config, qss=self._qss(), parent=self)
        if not dlg.exec():
            return
        self._log("设置已保存 → data/settings.json")
        try:
            from market_sniper.data.sources import ccxt_crypto, yfinance_us
            ccxt_crypto.reset_exchanges()
            yfinance_us.reset_session()
        except Exception:
            pass
        if self._engine is not None and self._engine.running:
            self._log("重启实时引擎以应用新设置…")
            self.btn_live.setChecked(False)
            self.btn_live.setChecked(True)

    def _on_live_toggle(self, checked: bool):
        if checked:
            if not self._start_engine():
                self.btn_live.setChecked(False)
        else:
            self._stop_engine()

    def _start_engine(self) -> bool:
        from market_sniper.engine import LiveEngine
        from market_sniper.engine.demo import DemoStrategy

        backend = str(self.config.get("feed.backend", "binance")).lower()
        strategies = []
        if backend == "mock":
            strategies.append(DemoStrategy(self.config.get("strategy.params") or {}))
        elif self.config.get("strategy.enabled", False):
            self._log("策略已启用，但尚无注册的策略类（在 engine 里实现后接入）")

        self._engine = LiveEngine(self.config, strategies=strategies)
        self._engine.bus.on("signal", self._on_engine_signal_threadsafe)
        try:
            self._engine.start()
        except Exception as e:
            self._engine = None
            self._log(f"实时启动失败: {e}")
            self._refresh_feed_status()
            return False

        syms = ", ".join(self.config.feed_symbols())
        proxy = self.config.proxy_url() or "直连"
        self._log(f"实时启动: {backend} [{syms}] proxy={proxy}")
        self._refresh_feed_status()
        return True

    def _stop_engine(self):
        if self._engine is None:
            return
        self._engine.bus.off("signal", self._on_engine_signal_threadsafe)
        self._engine.stop()
        st = self._engine.status()
        self._log(f"实时停止: tick {st['ticks']} / 信号 {st['signals']}"
                  + (f" / 落盘 {st['flushed']}" if st["flushed"] else ""))
        self._engine = None
        self._refresh_feed_status()

    def _on_engine_signal_threadsafe(self, sig):
        # feed 线程 → 主线程
        self._sig_engine_signal.emit(sig.as_dict())

    def _on_engine_signal(self, d: dict):
        self._engine_signals.append(d)
        if len(self._engine_signals) > 500:
            self._engine_signals = self._engine_signals[-500:]
        self._log(f"⚡ {d['side'].upper()} {d['local']} @ {d['price']:.4f}"
                  f" · {d.get('strategy', '')} {d.get('reason', '')}")
        self._apply_markers()

    def _refresh_feed_status(self):
        if self._engine is not None and self._engine.running:
            st = self._engine.status()
            last = (time.strftime("%H:%M:%S", time.localtime(st["last_ts_ms"] / 1000))
                    if st["last_ts_ms"] else "-")
            self.sb_feed.setText(
                f"实时 ● {st['feed']} · tick {st['ticks']} · 信号 {st['signals']}"
                f" · 最新 {last}")
            self.sb_feed.setStyleSheet(f"color:{ACCENT};")
        else:
            self.sb_feed.setText("实时: ○ 停止")
            self.sb_feed.setStyleSheet(f"color:{AXIS_C};")

    def _apply_markers(self):
        bars = getattr(self, "_last_bars", None)
        if not bars or not self.config.get("ui.show_markers", True):
            self.kline.set_markers([])
            return
        ts_list = bars.get("dates") or bars.get("ts") or []
        ms_list = [self._parse_ts_ms(t) for t in ts_list]
        local = f"{self._current_market}:{self._current_code}"
        out = []
        for s in self._engine_signals:
            if s.get("local") != local:
                continue
            idx = -1
            for i, ms in enumerate(ms_list):
                if ms is None:
                    continue
                if ms <= s["ts_ms"]:
                    idx = i
                else:
                    break
            if idx >= 0:
                out.append({"index": idx, "price": s["price"], "side": s["side"]})
        self.kline.set_markers(out[-200:])

    @staticmethod
    def _parse_ts_ms(s) -> int | None:
        if not s:
            return None
        s = str(s)
        try:
            if len(s) == 10:
                dt = datetime.datetime.strptime(s, "%Y-%m-%d")
            else:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None

    def _set_show_n(self, n: int):
        self._show_n = n

    def _set_subplot(self, s: str):
        self._subplot = s

    def _on_boll_toggle(self, v: bool):
        self._show_boll = v
        self._rerender()

    def _on_ma_toggle(self, n: int, v: bool):
        self._ma_on[n] = v
        self._rerender()

    def _on_fold_toggle(self, v: bool):
        self._fold_sub = v
        # 通过 kline_widget 暴露的接口切换
        if hasattr(self.kline, "set_fold"):
            self.kline.set_fold(v)
        else:
            # 简单方法：直接调整 p2 / p3 的可见性
            self.kline.p2.setVisible(not v)
            self.kline.p3.setVisible(not v)

    def _rerender(self):
        if not hasattr(self, "_last_bars") or self._last_bars is None:
            return
        if self._current_timeframe == "ts":
            self._render_timeshare(self._last_bars)
        else:
            self._render(self._last_bars)

    # ---------------- 周期 / 分时 ----------------
    def _on_timeframe_changed(self, *_a):
        tf = self.tf_combo.currentData()
        if tf == self._current_timeframe:
            return
        self._current_timeframe = tf
        self._set_intraday_controls()
        self._after_symbol_changed()

    def _set_intraday_controls(self):
        """分时模式下 MA/BOLL/副图不适用，禁用相关控件。"""
        ts = self._current_timeframe == "ts"
        self.sub_combo.setEnabled(not ts)
        self.boll_chk.setEnabled(not ts)
        for chk in self.ma_chks.values():
            chk.setEnabled(not ts)

    def _tf_label(self, tf: str) -> str:
        return dict(self.TIMEFRAMES).get(tf, tf)

    def _load_bars_for(self, tf: str) -> dict | None:
        market, code = self._current_market, self._current_code
        if tf == "1d":
            return dbmod.load_daily_bars(market, code, limit=self._show_n)
        if tf == "ts":
            return self._load_timeshare(market, code)
        return dbmod.load_min_bars(market, code, tf, limit=self._show_n)

    def _load_timeshare(self, market: str, code: str) -> dict | None:
        """分时数据：crypto 取最近 24h 的 1m；港美取最后一个交易日的 1m。"""
        last = dbmod.last_ts(market, code, "1m")
        if not last:
            return None
        if market == "CRYPTO":
            return dbmod.load_min_bars(market, code, "1m", limit=1440)
        day = last[:10]
        return dbmod.load_min_bars(market, code, "1m",
                                   start=f"{day}T00:00:00",
                                   end=f"{day}T23:59:59")

    def _timeshare_prev_close(self, bars: dict) -> float:
        """分时基准价：crypto 用窗口首根开盘（24h 口径）；港美用昨收日K。"""
        if self._current_market == "CRYPTO":
            if len(bars.get("open", [])):
                return float(bars["open"][0])
            return 0.0
        ts_list = bars.get("ts") or []
        day = ts_list[0][:10] if ts_list else ""
        try:
            daily = dbmod.load_daily_bars(self._current_market,
                                          self._current_code, limit=2)
        except Exception:
            daily = None
        if daily and len(daily["close"]):
            dates = daily["dates"]
            if dates[-1] == day and len(dates) >= 2:
                return float(daily["close"][-2])
            if dates[-1] != day:
                return float(daily["close"][-1])
        if len(bars.get("open", [])):
            return float(bars["open"][0])
        return 0.0

    def _render_timeshare(self, bars: dict):
        closes = bars["close"]
        vols = np.where(np.isnan(bars["volume"]), 0.0, bars["volume"])
        cv = np.cumsum(closes * vols)
        vv = np.cumsum(vols)
        avg = np.where(vv > 0, cv / np.maximum(vv, 1e-9), closes)
        prev_close = self._timeshare_prev_close(bars)
        ind = ind_mod.compute(bars)
        stock = dbmod.get_stock(self._current_market, self._current_code)
        name = (stock or {}).get("name") or ""
        data = KlineData(
            market=self._current_market,
            code=self._current_code,
            timeframe="分时",
            dates=list(bars.get("ts") or []),
            opens=bars["open"], highs=bars["high"],
            lows=bars["low"], closes=closes, volumes=bars["volume"],
            indicators=ind, name=name,
            mode="timeshare", avg=avg, prev_close=prev_close,
        )
        self._last_bars = bars
        self.kline.set_data(data)
        self._apply_markers()
        self._refresh_quote_box(ind, name)
        self._update_info()
        self.sb_msg.setText(
            f"已加载 {self._current_market}:{self._current_code} "
            f"分时 · {len(closes)} 根"
        )

    def _after_symbol_changed(self):
        market = self._current_market
        code = self._current_code
        tf = self._current_timeframe
        self._update_info()
        self._log(f"加载 {market}:{code} {self._tf_label(tf)}…")

        bars = self._load_bars_for(tf)
        min_need = 10 if tf == "ts" else 30

        if not bars or len(bars.get("close", [])) < min_need:
            self._log(f"  本地数据不足（{len(bars.get('close', [])) if bars else 0} 根），后台拉取…")
            self._worker = FetchWorker(f"{market}:{code}", tf)
            self._thread = QtCore.QThread(self)
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            self._worker.finished.connect(self._after_fetch)
            self._worker.failed.connect(self._on_fetch_failed)
            self._worker.finished.connect(self._thread.quit)
            self._worker.failed.connect(self._thread.quit)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.start()
            return

        if tf == "ts":
            self._render_timeshare(bars)
        else:
            self._render(bars)

    def _after_fetch(self, local: str):
        market, code = local.split(":", 1)
        if local != f"{self._current_market}:{self._current_code}":
            return
        bars = self._load_bars_for(self._current_timeframe)
        if bars:
            if self._current_timeframe == "ts":
                self._render_timeshare(bars)
            else:
                self._render(bars)
        self._refresh_health()

    def _on_fetch_failed(self, msg: str):
        self._log(f"拉取失败: {msg}")

    def _render(self, bars: dict):
        self._last_bars = bars
        ind = ind_mod.compute(bars)
        # 应用 BOLL / MA 开关
        for col in ("boll_upper", "boll_mid", "boll_lower"):
            if not self._show_boll and col in ind:
                ind[col] = np.full_like(ind[col], np.nan)
        for n in MA_COLORS:
            if not self._ma_on.get(n, True):
                col = f"ma{n}"
                if col in ind:
                    ind[col] = np.full_like(ind[col], np.nan)

        stock = dbmod.get_stock(self._current_market, self._current_code)
        name = (stock or {}).get("name") or ""
        dates = bars.get("dates")
        if dates is None:
            dates = bars.get("ts")
        data = KlineData(
            market=self._current_market,
            code=self._current_code,
            timeframe=self._current_timeframe,
            dates=list(dates or []),
            opens=bars["open"], highs=bars["high"],
            lows=bars["low"], closes=bars["close"],
            volumes=bars["volume"], indicators=ind, name=name,
        )
        self.kline.set_data(data)
        self.kline.set_subplot(self._subplot)
        self._apply_markers()
        self._refresh_quote_box(ind, name)
        self._update_info()
        self.sb_msg.setText(
            f"已加载 {self._current_market}:{self._current_code} "
            f"{self._current_timeframe} · {len(data.closes)} 根"
        )

    def _refresh_quote_box(self, ind: dict, name: str):
        market = self._current_market
        code = self._current_code
        n = len(ind.get("close", [])) if ind else 0
        if not n:
            self.quote_box.setText("（无数据）")
            return
        c = ind["close"][-1]
        prev = ind["close"][-2] if n >= 2 else c
        chg_pct = (c - prev) / prev * 100 if prev else 0.0
        chg_color = UP if chg_pct >= 0 else DOWN
        chg_sign = "+" if chg_pct >= 0 else ""
        html = (
            f"<div style='font-size:11pt;'>"
            f"<b>{market}:{code}</b> <span style='color:{AXIS_C};'>{name}</span>"
            f"</div>"
            f"<div style='font-size:14pt; margin:4px 0;'>"
            f"<b style='color:{FG_MAIN};'>{c:.2f}</b>  "
            f"<b style='color:{chg_color};'>{chg_sign}{chg_pct:.2f}%</b>"
            f"</div>"
            f"<div style='color:{AXIS_C}; font-size:8pt;'>"
            f"O {ind['open'][-1]:.2f}  H {ind['high'][-1]:.2f}  "
            f"L {ind['low'][-1]:.2f}  P {prev:.2f}"
            f"</div>"
        )
        self._quote_html = html
        self.quote_box.setText(html)

        lv = ind_mod.last_values(ind, [
            "ma5", "ma10", "ma20", "ma30", "ma60",
            "ema12", "ema26", "macd_dif", "macd_dea", "macd_hist",
            "boll_upper", "boll_mid", "boll_lower",
            "kdj_k", "kdj_d", "kdj_j",
            "rsi_6", "rsi_14", "rsi_24", "atr_14",
        ])
        rows_fmt = []
        for k, v in lv.items():
            if v is None:
                continue
            color = FG_MAIN
            if k.startswith(("macd_", "kdj_", "rsi_")) and v < 0:
                color = DOWN
            rows_fmt.append(
                f"<span style='color:{AXIS_C};'>{k:12s}</span>  "
                f"<span style='color:{color};'>{v:+.4f}</span>"
            )
        self.ind_label.setText(
            "<br>".join(rows_fmt) if rows_fmt else "无指标"
        )

        # 在线报价走后台线程（HTTP 较慢，不能阻塞 UI）
        local = f"{market}:{code}"

        def work():
            try:
                q = fetcher.quote(local) or {}
            except Exception as e:
                q = {"_error": str(e)}
            self._sig_quote.emit(local, q)

        threading.Thread(target=work, daemon=True, name="quote").start()

    def _apply_quote(self, local: str, q: dict):
        # 只在标的未变时回填，避免旧结果覆盖新标的
        if local != f"{self._current_market}:{self._current_code}":
            return
        html = getattr(self, "_quote_html", "")
        if not html:
            return
        if q and q.get("day_high"):
            html += (
                f"<div style='color:{AXIS_C}; font-size:8pt;'>"
                f"日高 {q['day_high']:.2f}  日低 {q['day_low']:.2f}"
                f"</div>"
            )
        if q and q.get("volume"):
            html += (
                f"<div style='color:{AXIS_C}; font-size:8pt;'>"
                f"量 {q['volume']:,.0f}"
                f"</div>"
            )
        self.quote_box.setText(html)

    def _update_info(self):
        market = self._current_market
        code = self._current_code
        stock = dbmod.get_stock(market, code) or {}
        name = stock.get("name") or ""
        n = dbmod.count_bars(market, code, daily=True) if self._current_timeframe == "1d" else dbmod.count_bars(market, code, daily=False)
        if self._current_timeframe == "1d":
            last = dbmod.last_date(market, code)
        else:
            last = dbmod.last_ts(market, code, "1m" if self._current_timeframe == "ts" else self._current_timeframe)
        exch = stock.get("exchange") or ""
        tf_label = self._tf_label(self._current_timeframe)
        self.info_lbl.setText(
            f"  {market}:{code}  {name}  {exch}  · {tf_label} {n} 根 · 最近 {last or '-'}  "
        )
        # 市场状态：开/闭/盘后
        self._update_phase()

    def _update_phase(self):
        # 简单按本机时间估
        h = datetime.datetime.now().hour
        if 9 <= h < 12 or 13 <= h < 15:
            self.phase_var.setText("市场状态 ●  交易中")
        elif 12 <= h < 13:
            self.phase_var.setText("市场状态 ●  午间休市")
        else:
            self.phase_var.setText("市场状态 ●  盘后")

    def _refresh_indices(self):
        """后台线程串行查 5 个指数（HTTP 较慢，不能阻塞 UI）。"""
        if self._indices_busy:
            return
        self._indices_busy = True

        def work():
            out = {}
            for local, _name in INDEX_CODES:
                try:
                    out[local] = fetcher.quote(local) or {}
                except Exception:
                    out[local] = {}
            self._sig_indices.emit(out)

        threading.Thread(target=work, daemon=True, name="idx-refresh").start()

    def _apply_indices(self, quotes: dict):
        self._indices_busy = False
        for local, _name in INDEX_CODES:
            q = quotes.get(local)
            if not q or q.get("last") is None:
                continue
            prev = q.get("prev_close") or q.get("last") or 0
            chg = (q["last"] - prev) / prev * 100 if prev else 0
            sign = "+" if chg >= 0 else ""
            color = UP if chg >= 0 else DOWN
            self.idx_labels[local][0].setText(f"{q['last']:.2f}")
            self.idx_labels[local][1].setTextFormat(
                QtCore.Qt.TextFormat.RichText
            )
            self.idx_labels[local][1].setText(
                f"<span style='color:{color};'>{sign}{chg:.2f}%</span>"
            )

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    def _default_market_html(self) -> str:
        return (
            f"<div style='color:{AXIS_C}; font-size:8pt; line-height:1.4;'>"
            f"· 数据源：HK/US/CRYPTO 全部在线<br/>"
            f"· 工作时间：crypto 24×7；HK 09:30-16:00；<br/>"
            f"  US 21:30-04:00（冬令时 22:30-05:00）<br/>"
            f"· ⚙ 设置：数据源/代理/引擎参数<br/>"
            f"· ● 实时：Mock 离线联调 / Binance WS<br/>"
            f"· F11 全屏 · Esc 退出<br/>"
            f"</div>"
        )


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())