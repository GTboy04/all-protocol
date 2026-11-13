"""Roomy, detachable request list and packet reader shared by both roles."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter,
    QVBoxLayout, QWidget,
)


class JournalView(QWidget):
    def __init__(self, table, detail, title):
        super().__init__()
        self._title = title
        self._dialog = None
        self._manual_orientation = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toolbar = QWidget()
        actions = self.actions = QHBoxLayout(self.toolbar)
        actions.setContentsMargins(0, 0, 0, 0)
        self.orientation_button = QPushButton("左右布局")
        self.orientation_button.setToolTip("切换日志列表与报文的排列方向")
        self.orientation_button.clicked.connect(self._toggle_orientation)
        actions.addWidget(self.orientation_button)
        self.wrap_button = QPushButton("自动换行")
        self.wrap_button.setCheckable(True)
        self.wrap_button.setChecked(True)
        self.wrap_button.toggled.connect(lambda checked: detail.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap))
        actions.addWidget(self.wrap_button)
        actions.addStretch()
        self.expand_button = QPushButton("独立窗口…")
        self.expand_button.setToolTip("在可最大化的独立窗口中阅读日志，关闭后回到原位置")
        self.expand_button.clicked.connect(self._toggle_window)
        actions.addWidget(self.expand_button)
        layout.addWidget(self.toolbar)
        detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        detail.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        detail.setMinimumHeight(64)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        self.split.addWidget(table)
        self.split.addWidget(detail)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([180, 120])
        layout.addWidget(self.split, 1)
        self.placeholder = QPushButton("日志已在独立窗口打开 · 点击显示")
        self.placeholder.clicked.connect(self.open_window)
        layout.addWidget(self.placeholder)
        self.placeholder.hide()

    def _toggle_orientation(self):
        self._manual_orientation = True
        self._set_orientation(self.split.orientation() == Qt.Orientation.Vertical)

    def _set_orientation(self, horizontal):
        orientation = Qt.Orientation.Horizontal if horizontal else Qt.Orientation.Vertical
        if self.split.orientation() == orientation:
            return
        self.split.setOrientation(orientation)
        self.orientation_button.setText("上下布局" if horizontal else "左右布局")
        self.split.setSizes([600, 400] if horizontal else [180, 120])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt_layout()

    def _adapt_layout(self):
        if not self._manual_orientation and self._dialog is None:
            # Shallow bottom panels need side-by-side panes to retain useful
            # row counts; a focused reader has room for full-width packets.
            self._set_orientation(self.width() >= 950 and self.height() < 420)

    def open_window(self):
        if self._dialog is None:
            self._inline_sizes = self.split.sizes()
            dialog = QDialog(self)
            dialog.setWindowTitle(self._title)
            dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
            dialog.resize(1180, 780)
            if not self._manual_orientation:
                self._set_orientation(False)
            body = QVBoxLayout(dialog)
            body.setContentsMargins(16, 16, 16, 16)
            body.addWidget(self.toolbar)
            # Move the live widgets, preserving selection, scroll and updates.
            body.addWidget(self.split)
            dialog.finished.connect(self._restore)
            self._dialog = dialog
            self.placeholder.show()
            self.expand_button.setText("返回工作区")
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _toggle_window(self):
        if self._dialog is not None:
            self._dialog.close()
        else:
            self.open_window()

    def _restore(self, _result):
        dialog, self._dialog = self._dialog, None
        self.layout().insertWidget(0, self.toolbar)
        self.toolbar.show()
        self.layout().insertWidget(1, self.split, 1)
        self.split.show()
        self.split.setSizes(self._inline_sizes)
        self.placeholder.hide()
        self.expand_button.setText("独立窗口…")
        self._adapt_layout()
        dialog.deleteLater()
