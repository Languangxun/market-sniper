# -*- coding: utf-8 -*-
"""回填对话框：代码（分号分隔，留空=整个候选池） + 周期 + 根数/天数。"""
from __future__ import annotations

from PyQt6 import QtWidgets

_PREFIXES = ("HK", "US", "CRYPTO")


class BackfillDialog(QtWidgets.QDialog):
    def __init__(self, *, current_market: str, current_code: str,
                 timeframes, qss: str = "", parent=None):
        super().__init__(parent)
        self._market = current_market
        self.setWindowTitle("批量回填")
        self.resize(480, 220)
        if qss:
            self.setStyleSheet(qss)

        root = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.codes_edit = QtWidgets.QLineEdit()
        self.codes_edit.setPlaceholderText(
            f"HK:00700; US:AAPL; BTC/USDT（分号分隔，留空 = {current_market} 整个候选池）")
        form.addRow("代码", self.codes_edit)

        self.tf_combo = QtWidgets.QComboBox()
        for tf, label in timeframes:
            if tf == "ts":          # 分时由 1m 派生，不单独回填
                continue
            self.tf_combo.addItem(label, tf)
        idx = self.tf_combo.findData("1d")
        self.tf_combo.setCurrentIndex(max(0, idx))
        form.addRow("周期", self.tf_combo)

        self.bars_spin = QtWidgets.QSpinBox()
        self.bars_spin.setRange(5, 100000)
        self.bars_spin.setSingleStep(50)
        self.bars_spin.setValue(250)
        self.bars_spin.setSuffix(" 根")
        form.addRow("根数/天数", self.bars_spin)

        self.force_chk = QtWidgets.QCheckBox("强制重拉（忽略本地最新日期）")
        form.addRow("", self.force_chk)

        hint = QtWidgets.QLabel(
            "日K 按「天数」回填；分钟K 按根数取最近档位——yfinance 1m 上限 7d、"
            "5m/15m/30m 上限 60d、60m 上限 730d，crypto 直接按根数拉。")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        root.addLayout(form)
        root.addWidget(hint)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("开始回填")
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # -------- 结果 --------
    def codes(self) -> list[str]:
        """解析输入：分号/逗号/空格分隔；自动识别市场（crypto 斜杠、
        纯数字→HK、字母→US），识别不了补当前市场。"""
        from market_sniper.symbols import infer_market
        raw = (self.codes_edit.text() or "").strip()
        if not raw:
            return []
        parts = (raw.replace("；", ";").replace("，", ";")
                 .replace(",", ";").replace(" ", ";").split(";"))
        out = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if ":" in p and p.split(":", 1)[0].upper() in _PREFIXES:
                out.append(p.upper())
                continue
            mkt = infer_market(p)
            if mkt not in _PREFIXES:
                mkt = self._market
            out.append(f"{mkt}:{p.upper() if mkt != 'HK' else p}")
        return out

    def timeframe(self) -> str:
        return self.tf_combo.currentData()

    def bars(self) -> int:
        return self.bars_spin.value()

    def force(self) -> bool:
        return self.force_chk.isChecked()
