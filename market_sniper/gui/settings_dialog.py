# -*- coding: utf-8 -*-
"""设置面板：数据源 / 网络代理 / 引擎 / 策略参数（写入 data/settings.json）。"""
from __future__ import annotations

import json

from PyQt6 import QtWidgets

from market_sniper import config as config_mod

_BACKENDS = (
    ("binance", "Binance 实时 (WebSocket)"),
    ("mock", "离线模拟 (Mock)"),
)

# 各市场可选数据源（key 与 config.sources 对应）
_SOURCE_OPTIONS = {
    "HK": (("yfinance", "Yahoo Finance（延迟约15分）"),),
    "US": (("yfinance", "Yahoo Finance（延迟约15分）"),),
    "CRYPTO": (("binance", "Binance"), ("okx", "OKX"),
               ("bybit", "Bybit"), ("gate", "Gate")),
}


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, config: config_mod.Config, *, qss: str = "", parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置 · market-sniper")
        self.resize(580, 560)
        if qss:
            self.setStyleSheet(qss)
        self._build()
        self._load()

    # -------- UI --------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs, 1)

        # ---- 数据源 / 网络 ----
        data_w = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(data_w)

        src_box = QtWidgets.QGroupBox("历史/延时数据源（HK/US 免费源延迟约15分钟）")
        sf = QtWidgets.QFormLayout(src_box)
        self.src_combos: dict[str, QtWidgets.QComboBox] = {}
        for mkt in ("HK", "US", "CRYPTO"):
            combo = QtWidgets.QComboBox()
            for key, label in _SOURCE_OPTIONS[mkt]:
                combo.addItem(label, key)
            sf.addRow(mkt, combo)
            self.src_combos[mkt] = combo
        dv.addWidget(src_box)

        form = QtWidgets.QFormLayout()

        self.backend_combo = QtWidgets.QComboBox()
        for key, label in _BACKENDS:
            self.backend_combo.addItem(label, key)
        self.backend_combo.currentIndexChanged.connect(self._update_proxy_hint)
        form.addRow("行情后端", self.backend_combo)

        self.symbols_edit = QtWidgets.QLineEdit()
        self.symbols_edit.setPlaceholderText("BTC/USDT, ETH/USDT, SOL/USDT")
        form.addRow("实时标的", self.symbols_edit)

        dv.addLayout(form)

        stream_box = QtWidgets.QGroupBox("订阅流")
        sh = QtWidgets.QHBoxLayout(stream_box)
        self.chk_agg = QtWidgets.QCheckBox("逐笔成交 aggTrade")
        self.chk_kline = QtWidgets.QCheckBox("1s K线")
        self.chk_book = QtWidgets.QCheckBox("盘口 bookTicker")
        for c in (self.chk_agg, self.chk_kline, self.chk_book):
            sh.addWidget(c)
        dv.addWidget(stream_box)

        net_box = QtWidgets.QGroupBox("网络代理")
        nf = QtWidgets.QFormLayout(net_box)
        self.chk_proxy = QtWidgets.QCheckBox("启用代理")
        nf.addRow("", self.chk_proxy)
        self.proxy_host = QtWidgets.QLineEdit()
        nf.addRow("主机", self.proxy_host)
        self.proxy_port = QtWidgets.QSpinBox()
        self.proxy_port.setRange(1, 65535)
        nf.addRow("端口", self.proxy_port)
        self.timeout_spin = QtWidgets.QSpinBox()
        self.timeout_spin.setRange(1, 120)
        self.timeout_spin.setSuffix(" 秒")
        nf.addRow("超时", self.timeout_spin)
        self.proxy_hint = QtWidgets.QLabel("")
        self.proxy_hint.setObjectName("subtle")
        nf.addRow("", self.proxy_hint)
        self.chk_proxy.toggled.connect(self._update_proxy_hint)
        dv.addWidget(net_box)
        dv.addStretch(1)
        tabs.addTab(data_w, "数据源 / 网络")

        # ---- 引擎 ----
        eng_w = QtWidgets.QWidget()
        ef = QtWidgets.QFormLayout(eng_w)
        self.ring_spin = QtWidgets.QSpinBox()
        self.ring_spin.setRange(100, 1_000_000)
        self.ring_spin.setSingleStep(500)
        self.ring_spin.setSuffix(" 条/标的")
        ef.addRow("内存环形缓冲", self.ring_spin)
        self.persist_chk = QtWidgets.QCheckBox("逐笔落盘（ticks 表）")
        ef.addRow("", self.persist_chk)
        self.flush_spin = QtWidgets.QSpinBox()
        self.flush_spin.setRange(50, 60000)
        self.flush_spin.setSuffix(" ms")
        ef.addRow("落盘批量间隔", self.flush_spin)
        self.reconnect_spin = QtWidgets.QSpinBox()
        self.reconnect_spin.setRange(5, 600)
        self.reconnect_spin.setSuffix(" 秒")
        ef.addRow("断线重连上限", self.reconnect_spin)
        self.markers_chk = QtWidgets.QCheckBox("在 K 线叠加买卖点")
        ef.addRow("", self.markers_chk)
        tabs.addTab(eng_w, "实时引擎")

        # ---- 策略 ----
        st_w = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(st_w)
        self.strategy_chk = QtWidgets.QCheckBox("启用策略")
        sv.addWidget(self.strategy_chk)
        hint = QtWidgets.QLabel(
            "策略参数（JSON）。算法在 market_sniper/engine/strategy.py 的\n"
            "Strategy 子类里实现 on_tick / on_bar，返回 Signal 即触发买卖点。"
        )
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        sv.addWidget(hint)
        self.strategy_params = QtWidgets.QPlainTextEdit()
        self.strategy_params.setPlaceholderText('{\n  "every": 40\n}')
        sv.addWidget(self.strategy_params, 1)
        tabs.addTab(st_w, "策略参数")

        # ---- 按钮 ----
        btns = QtWidgets.QDialogButtonBox()
        reset_btn = btns.addButton("恢复默认",
                                   QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
        btns.addButton("取消", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        btns.addButton("保存", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        reset_btn.clicked.connect(self._on_reset)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # -------- 载入 / 收集 --------
    def _load(self):
        c = self.config
        for mkt, combo in self.src_combos.items():
            idx = combo.findData(c.get(f"sources.{mkt}"))
            combo.setCurrentIndex(max(0, idx))
        idx = self.backend_combo.findData(c.get("feed.backend", "binance"))
        self.backend_combo.setCurrentIndex(max(0, idx))
        self.symbols_edit.setText(", ".join(c.feed_symbols()))
        streams = c.get("feed.streams") or {}
        self.chk_agg.setChecked(bool(streams.get("agg_trade", True)))
        self.chk_kline.setChecked(bool(streams.get("kline_1s", True)))
        self.chk_book.setChecked(bool(streams.get("book_ticker", True)))

        self.chk_proxy.setChecked(bool(c.get("network.proxy_enabled", True)))
        self.proxy_host.setText(str(c.get("network.proxy_host", "127.0.0.1")))
        self.proxy_port.setValue(int(c.get("network.proxy_port", 7890)))
        self.timeout_spin.setValue(int(c.get("network.timeout", 15)))

        self.ring_spin.setValue(int(c.get("feed.ring_size", 5000)))
        self.persist_chk.setChecked(bool(c.get("feed.persist_ticks", False)))
        self.flush_spin.setValue(int(c.get("feed.flush_ms", 1000)))
        self.reconnect_spin.setValue(int(c.get("feed.reconnect_max_sec", 30)))
        self.markers_chk.setChecked(bool(c.get("ui.show_markers", True)))

        self.strategy_chk.setChecked(bool(c.get("strategy.enabled", False)))
        self.strategy_params.setPlainText(
            json.dumps(c.get("strategy.params") or {}, ensure_ascii=False, indent=2))
        self._update_proxy_hint()

    def _update_proxy_hint(self):
        if not self.chk_proxy.isChecked():
            self.proxy_hint.setText("代理已关闭")
            return
        host = self.proxy_host.text().strip() or "127.0.0.1"
        self.proxy_hint.setText(
            f"生效：http://{host}:{self.proxy_port.value()}（WebSocket/HTTP 共用）")

    def _collect(self) -> dict | None:
        raw = self.strategy_params.toPlainText().strip() or "{}"
        try:
            params = json.loads(raw)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "策略参数", f"JSON 解析失败：{e}")
            return None
        if not isinstance(params, dict):
            QtWidgets.QMessageBox.warning(self, "策略参数", "参数必须是 JSON 对象 {}")
            return None
        symbols = [s.strip().upper()
                   for s in self.symbols_edit.text().replace(";", ",").split(",")
                   if s.strip()]
        if not symbols:
            QtWidgets.QMessageBox.warning(self, "实时标的", "至少填一个标的")
            return None
        return {
            "sources": {
                mkt: combo.currentData() for mkt, combo in self.src_combos.items()
            },
            "network": {
                "proxy_enabled": self.chk_proxy.isChecked(),
                "proxy_host": self.proxy_host.text().strip() or "127.0.0.1",
                "proxy_port": self.proxy_port.value(),
                "timeout": self.timeout_spin.value(),
            },
            "feed": {
                "backend": self.backend_combo.currentData(),
                "symbols": symbols,
                "streams": {
                    "agg_trade": self.chk_agg.isChecked(),
                    "kline_1s": self.chk_kline.isChecked(),
                    "book_ticker": self.chk_book.isChecked(),
                },
                "ring_size": self.ring_spin.value(),
                "persist_ticks": self.persist_chk.isChecked(),
                "flush_ms": self.flush_spin.value(),
                "reconnect_max_sec": self.reconnect_spin.value(),
            },
            "strategy": {
                "enabled": self.strategy_chk.isChecked(),
                "params": params,
            },
            "ui": {
                "show_markers": self.markers_chk.isChecked(),
            },
        }

    # -------- 动作 --------
    def _on_save(self):
        data = self._collect()
        if data is None:
            return
        self.config.update(data)
        try:
            self.config.save()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(e))
            return
        self.accept()

    def _on_reset(self):
        self.config.reset()
        self._load()
