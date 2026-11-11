"""Shared live, read-only bit inspector for client and simulated device values."""
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QFrame, QVBoxLayout, QWidget,
)

from ..application.binary import BitPattern


@dataclass(frozen=True)
class BitReading:
    label: str
    pattern: BitPattern | None
    context: str = ""


class BitInspector(QDialog):
    def __init__(self, parent, provider):
        super().__init__(parent)
        self.setWindowTitle("二进制 / Bit 位查看")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(720, 440)
        self.provider = provider
        self._last = None
        self.selected_bit = 0
        self.cells = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.heading = QLabel(objectName="sectionTitle")
        self.context = QLabel(objectName="resultContext")
        self.context.setWordWrap(True)
        self.binary = QLabel(objectName="binaryValue")
        self.binary.setWordWrap(True)
        self.binary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for label in (self.heading, self.context, self.binary):
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        grid_widget = QWidget()
        self.grid = QGridLayout(grid_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.bit_scroll = QScrollArea()
        self.bit_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.bit_scroll.setWidgetResizable(True)
        self.bit_scroll.setWidget(grid_widget)
        layout.addWidget(self.bit_scroll)
        self.detail = QLabel(objectName="permissions")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        explanation = QLabel("左侧为高位，bit 0 是最低位。显示解码后数值的位模式，不代表 TX/RX 字节顺序。\n"
                             "整数负数使用补码，浮点数使用 IEEE 754。业务含义请查阅设备点表。", objectName="resultContext")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addStretch()
        actions = QHBoxLayout()
        self.copy = QPushButton("复制二进制")
        self.copy.clicked.connect(self._copy)
        actions.addWidget(self.copy)
        actions.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.close)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        self.timer = QTimer(self, interval=200)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        reading = self.provider()
        if reading == self._last:
            return
        self._last = reading
        pattern = reading.pattern if reading else None
        self.heading.setText(reading.label if reading else "请选择一个点位")
        self.context.setText(reading.context if reading else "在原窗口选择结果或点位后，可在这里逐位查看。")
        self.binary.setText(f"{pattern.kind} · {pattern.width} bit · {pattern.hexadecimal}\n{pattern.binary}" if pattern
                            else "暂无可显示的位模式；请先读取数值。非有限浮点数无法从解码值还原原始位模式。")
        width = pattern.width if pattern else 0
        available_height = self.screen().availableGeometry().height()
        max_grid_height = max(112, min(320, available_height - 400))
        self.bit_scroll.setFixedHeight(min(max_grid_height, max(64, ((width + 7) // 8) * 58)))
        if len(self.cells) != width:
            while self.cells:
                self.cells.pop().deleteLater()
            for position, bit in enumerate(range(width - 1, -1, -1)):
                cell = QPushButton(objectName="bitCell")
                cell.clicked.connect(lambda _=False, index=bit: self._select(index))
                self.grid.addWidget(cell, position // 8, position % 8)
                self.cells.append(cell)
        for cell, bit in zip(self.cells, range(width - 1, -1, -1)):
            value = pattern.bit(bit)
            cell.setText(f"bit {bit}\n{value}")
            cell.setAccessibleName(f"bit {bit} = {value}")
            cell.setToolTip(f"bit {bit} = {value}；掩码 0x{1 << bit:X}")
            cell.setProperty("bitValue", str(value))
            cell.style().unpolish(cell)
            cell.style().polish(cell)
        self.copy.setEnabled(pattern is not None)
        self._select(min(self.selected_bit, max(0, width - 1)))

    def _select(self, index):
        self.selected_bit = index
        pattern = self._last.pattern if self._last else None
        if pattern is None:
            self.detail.setText("位查看为只读，不会修改设备。")
            return
        extra = ""
        if pattern.kind in ("Float32", "Real", "Float64"):
            mantissa = 52 if pattern.width == 64 else 23
            extra = "；符号位" if index == pattern.width - 1 else "；指数位" if index >= mantissa else "；尾数位"
        elif pattern.kind in ("Int", "DInt", "Int16", "Int32", "Int64") and index == pattern.width - 1:
            extra = "；补码符号位"
        self.detail.setText(f"bit {index} = {pattern.bit(index)}（{'置位' if pattern.bit(index) else '清零'}）"
                            f"；掩码 0x{1 << index:X}{extra}。未定义业务含义。")

    def _copy(self):
        if self._last and self._last.pattern:
            QApplication.clipboard().setText(self._last.pattern.binary)


def show_bit_inspector(parent, provider):
    dialog = getattr(parent, "_bit_inspector", None)
    if dialog is not None:
        # The reference is cleared on destruction, including parent shutdown.
        dialog.raise_()
        dialog.activateWindow()
        return
    dialog = BitInspector(parent, provider)
    parent._bit_inspector = dialog
    dialog.destroyed.connect(lambda: setattr(parent, "_bit_inspector", None))
    dialog.show()
