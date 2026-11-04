import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget,
)

from ..adapters.modbus_tcp.model import RULES, Area, ModbusTarget, ModbusTcpConfig
from ..application.contracts import ClientError, Command, Target, Value
from .widgets import add_field


def spinbox(minimum, maximum, value):
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    box.setKeyboardTracking(False)
    return box


class ModbusTcpForm:
    def __init__(self):
        self.connection = _ConnectionForm()
        self.query = _QueryForm()

    def connection_config(self) -> tuple[object, str]:
        host = self.connection.host.text().strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        config = ModbusTcpConfig(host, self.connection.port.value(), self.connection.timeout.value())
        config.validate()
        return config, config.endpoint

    def target(self) -> ModbusTarget:
        return self.query.target()

    def can_write(self) -> bool:
        target = self.target()
        return 1 <= target.count <= RULES[target.area].write_limit

    def confirm_write(self, parent: QWidget, endpoint: str, target: Target) -> tuple[Value, ...] | None:
        dialog = _WriteDialog(parent, endpoint, target)
        return dialog.values if dialog.exec() == QDialog.DialogCode.Accepted else None


class _ConnectionForm(QWidget):
    def __init__(self):
        super().__init__()
        self.host = QLineEdit()
        self.host.setPlaceholderText("例如 127.0.0.1")
        self.port = spinbox(1, 65535, 502)
        self.timeout = spinbox(100, 30000, 3000)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        add_field(layout, "主机", self.host, 1)
        add_field(layout, "端口", self.port)
        add_field(layout, "超时（ms）", self.timeout)


class _QueryForm(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.unit = spinbox(1, 255, 1)
        self.unit.setToolTip("按设备手册设置站号。TCP 直连设备可能使用 255。")
        self.area = QComboBox()
        for area, rule in RULES.items():
            self.area.addItem(f"{area} {rule.label}", area.value)
        self.offset = spinbox(0, 65535, 0)
        self.count = spinbox(1, 125, 4)
        self.permissions = QLabel()
        self.permissions.setObjectName("permissions")
        self.permissions.setWordWrap(True)
        fields = QHBoxLayout()
        fields.setSpacing(10)
        add_field(fields, "Unit ID", self.unit)
        add_field(fields, "数据区", self.area, 1)
        add_field(fields, "起始偏移", self.offset)
        add_field(fields, "数量", self.count)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(fields)
        layout.addWidget(self.permissions)
        self.area.currentIndexChanged.connect(self._changed)
        for box in (self.unit, self.offset, self.count):
            box.valueChanged.connect(self._changed)
        self._changed()

    def target(self) -> ModbusTarget:
        return ModbusTarget(self.unit.value(), Area(self.area.currentData()),
                            self.offset.value(), self.count.value())

    def _changed(self):
        rule = RULES[Area(self.area.currentData())]
        previous = self.count.blockSignals(True)
        self.count.setMaximum(min(rule.read_limit, 65536 - self.offset.value()))
        self.count.blockSignals(previous)
        operations = f"只读 · 读取 0x{rule.read_code:02X} · 最多 {rule.read_limit} 点"
        if rule.write_codes is not None:
            single, multiple = rule.write_codes
            operations = (
                f"可读写 · 读取 0x{rule.read_code:02X} · "
                f"写入 0x{single:02X} / 0x{multiple:02X} · 最多写 {rule.write_limit} 点"
            )
        self.permissions.setText(f"{operations} · 地址从 0 开始")
        self.changed.emit()


class _WriteDialog(QDialog):
    def __init__(self, parent: QWidget, endpoint: str, target: ModbusTarget):
        super().__init__(parent)
        self.setWindowTitle("确认写入")
        self.resize(480, 250)
        self.target = target
        self.values: tuple[Value, ...] | None = None
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入十进制值，用逗号、空格或换行分隔")
        self.input.setAccessibleName("写入值")
        self.error = QLabel()
        self.error.setObjectName("warning")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认写入")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("write")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("目标设备", QLabel(endpoint))
        form.addRow("点位范围", QLabel(target.label))
        form.addRow("值的数量", QLabel(str(target.count)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(self.input)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.input.setFocus()

    def accept(self):
        fields = re.split(r"[,，\s]+", self.input.toPlainText().strip())
        if any(not re.fullmatch(r"[0-9]{1,5}", field) for field in fields):
            self.error.setText("请输入十进制整数，用逗号、空格或换行分隔")
            return
        values = tuple(int(field) for field in fields)
        try:
            self.target.validate(values)
        except ClientError as error:
            self.error.setText(str(error))
            return
        self.values = values
        super().accept()
