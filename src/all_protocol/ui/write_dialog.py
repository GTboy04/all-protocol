import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit, QVBoxLayout,
)

from ..adapters.modbus_tcp.model import ModbusTarget
from ..adapters.s7.model import DataType as S7Type
from ..application.contracts import ClientError, Command, VerificationOptions
from .widgets import spinbox


class WriteDialog(QDialog):
    def __init__(self, parent, endpoint, target, point=None):
        super().__init__(parent)
        self.setWindowTitle("确认写入")
        self.resize(490, 420)
        self.target, self.point = target, point
        self.command = None
        modbus = isinstance(target, ModbusTarget)
        kind = target.value_type if modbus else target.data_type
        self.floating = kind.floating if modbus else kind == S7Type.REAL
        self.parse = kind.parse if modbus else float if self.floating else int
        self.input = QPlainTextEdit()
        self.input.setFixedHeight(62)
        self.input.setAccessibleName("写入值")
        self.input.setPlaceholderText("输入十进制值，用逗号、空格或换行分隔")
        self.preview = QPlainTextEdit()
        self.preview.setAccessibleName("写入值预览")
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(55 if point else 36)
        self.mode = None
        self.auto_verify = QCheckBox("设备应答后回读")
        self.auto_verify.setToolTip("延迟与回读期间，同一设备的其他请求在队列中等待")
        self.delay = spinbox(0, 5000, 0)
        self.delay.setSuffix(" ms")
        self.error = QLabel()
        self.error.setObjectName("warning")
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.hide()

        self.form = form = QFormLayout()
        device = QLabel(endpoint)
        device.setTextFormat(Qt.TextFormat.PlainText)
        device.setWordWrap(True)
        form.addRow("目标设备", device)
        label = QLabel(f"{point.name} · {target.label}" if point else target.label)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        form.addRow("点位范围", label)
        form.addRow("数据类型", QLabel(f"{kind} × {target.count}"))
        if modbus and kind.value != "Bool":
            form.addRow("寄存器顺序", QLabel(target.order.label))
        if point:
            self.mode = QComboBox()
            self.mode.addItems(("原始值", "工程值"))
            self.mode.currentIndexChanged.connect(self._preview)
            form.addRow("输入类型", self.mode)
            conversion = QLabel(f"原始值 × {point.scale} + {point.offset} {point.unit}".strip())
            conversion.setTextFormat(Qt.TextFormat.PlainText)
            conversion.setWordWrap(True)
            form.addRow("工程值换算", conversion)
        form.addRow("写入值", self.input)
        form.addRow("预览", self.preview)
        form.addRow("回读选项", self.auto_verify)
        form.addRow("回读延迟", self.delay)
        form.setRowVisible(self.delay, False)
        if self.floating:
            self.absolute = QLineEdit("1e-6")
            self.relative = QLineEdit("1e-6")
            form.addRow("绝对容差（原始值）", self.absolute)
            form.addRow("相对容差", self.relative)
        self.auto_verify.toggled.connect(self._toggle_delay)
        self.input.textChanged.connect(self._preview)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        write = buttons.button(QDialogButtonBox.StandardButton.Ok)
        write.setText("确认写入")
        write.setObjectName("write")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.input.setFocus()

    def _toggle_delay(self, checked):
        self.form.setRowVisible(self.delay, checked)

    def _values(self):
        text = self.input.toPlainText().strip()
        fields = re.split(r"[,，\s]+", text) if text else ()
        if len(fields) != self.target.count:
            raise ValueError(f"请输入 {self.target.count} 个值")
        engineering = self.mode is not None and self.mode.currentIndex() == 1
        parse = self.point.raw_from_engineering if engineering else self.parse
        try:
            raw = tuple(parse(field) for field in fields)
        except ValueError as error:
            raise ValueError(str(error) if engineering else "请输入有效的十进制数值") from error
        raw = self.target.prepare_write(raw)
        converted = tuple(self.point.engineering_text(value) for value in raw) if self.point is not None else ()
        return raw, converted, tuple(fields) if engineering else converted

    def _preview(self, _index=None):
        self.error.hide()
        try:
            raw, converted, _ = self._values()
        except (ClientError, ValueError):
            self.preview.clear()
            return
        text = "原始值  " + ", ".join(map(str, raw))
        if converted:
            text += "\n工程值  " + ", ".join(converted) + f" {self.point.unit}"
        self.preview.setPlainText(text)

    def accept(self):
        try:
            raw, _, engineering = self._values()
            delay = self.delay.value() if self.auto_verify.isChecked() else None
            options = None
            if self.floating:
                try:
                    absolute, relative = float(self.absolute.text()), float(self.relative.text())
                except ValueError as error:
                    raise ValueError("请输入有效的绝对和相对容差") from error
                options = VerificationOptions(delay, absolute, relative)
            elif delay is not None:
                options = VerificationOptions(delay)
        except (ClientError, ValueError) as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.command = Command(self.target, raw, options, engineering)
        super().accept()
