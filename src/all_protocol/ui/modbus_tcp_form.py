import re

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget,
)

from ..adapters.modbus_tcp.model import RULES, Area, ModbusTarget, ModbusTcpConfig, data_types
from ..adapters.modbus_tcp.values import DataType, RegisterOrder
from ..application.contracts import ClientError, Target, Value
from ..storage import ProfileFields
from .widgets import add_field, spinbox


def order_picker(selected=RegisterOrder.BIG):
    box = QComboBox()
    for order in RegisterOrder:
        box.addItem(order.label, order.value)
    box.setCurrentIndex(box.findData(selected.value))
    return box


class ModbusTcpForm:
    def __init__(self):
        self.connection = _ConnectionForm()
        self.query = _QueryForm()

    def connection_config(self) -> tuple[ModbusTcpConfig, str]:
        host = self.connection.host.text().strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        config = ModbusTcpConfig(host, self.connection.port.value(), self.connection.timeout.value())
        config.validate()
        return config, config.endpoint

    def profile_fields(self) -> ProfileFields:
        config, _ = self.connection_config()
        target = self.target()
        return {
            "host": config.host,
            "port": config.port,
            "timeout_ms": config.timeout_ms,
            "unit_id": target.unit_id,
            "area": target.area.value,
            "offset": target.offset,
            "count": target.count,
            "data_type": target.value_type.value,
            "order": target.order.value,
        }

    def restore_profile(self, fields: ProfileFields):
        host = fields["host"]
        port, timeout = fields["port"], fields["timeout_ms"]
        unit, offset, count = fields["unit_id"], fields["offset"], fields["count"]
        if not isinstance(host, str) or any(type(value) is not int for value in (port, timeout, unit, offset, count)):
            raise ValueError("无效的 Modbus TCP 连接记录")
        config = ModbusTcpConfig(host, port, timeout)
        area = Area(fields["area"])
        data_type = DataType(fields.get("data_type", data_types(area)[0]))
        order = RegisterOrder(fields.get("order", RegisterOrder.BIG))
        target = ModbusTarget(unit, area, offset, count, data_type, order)
        config.validate()
        target.validate()
        self.connection.host.setText(config.host)
        self.connection.port.setValue(config.port)
        self.connection.timeout.setValue(config.timeout_ms)
        self.query.unit.setValue(target.unit_id)
        self.query.area.setCurrentIndex(self.query.area.findData(target.area.value))
        self.query.data_type.setCurrentIndex(self.query.data_type.findData(data_type.value))
        self.query.order.setCurrentIndex(self.query.order.findData(order.value))
        self.query.offset.setValue(target.offset)
        self.query.count.setValue(target.count)

    def target(self) -> ModbusTarget:
        return self.query.target()

    def can_write(self) -> bool:
        target = self.target()
        return 1 <= target.register_count <= RULES[target.area].write_limit

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
        self.data_type = QComboBox()
        self.order = order_picker()
        self.permissions = QLabel()
        self.permissions.setObjectName("permissions")
        self.permissions.setWordWrap(True)
        fields = QHBoxLayout()
        fields.setSpacing(10)
        add_field(fields, "Unit ID", self.unit)
        add_field(fields, "数据区", self.area, 1)
        add_field(fields, "起始偏移", self.offset)
        add_field(fields, "点位数量", self.count)
        formats = QHBoxLayout()
        formats.setSpacing(10)
        add_field(formats, "数据类型", self.data_type)
        add_field(formats, "寄存器顺序", self.order)
        formats.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(fields)
        layout.addLayout(formats)
        layout.addWidget(self.permissions)
        self.area.currentIndexChanged.connect(self._area_changed)
        self.data_type.currentIndexChanged.connect(self._changed)
        self.order.currentIndexChanged.connect(self._changed)
        for box in (self.unit, self.offset, self.count):
            box.valueChanged.connect(self._changed)
        self._area_changed()

    def target(self) -> ModbusTarget:
        area = Area(self.area.currentData())
        data_type = DataType(self.data_type.currentData())
        order = RegisterOrder(self.order.currentData()) if data_type != DataType.BOOL else RegisterOrder.BIG
        return ModbusTarget(self.unit.value(), area, self.offset.value(), self.count.value(),
                            None if data_type == data_types(area)[0] else data_type, order)

    def _area_changed(self):
        supported = data_types(Area(self.area.currentData()))
        previous = self.data_type.currentData()
        with QSignalBlocker(self.data_type):
            self.data_type.clear()
            for data_type in supported:
                self.data_type.addItem(data_type.value, data_type.value)
            self.data_type.setCurrentIndex(max(0, self.data_type.findData(previous)))
        self._changed()

    def _changed(self):
        target = self.target()
        rule = RULES[target.area]
        words = target.value_type.words
        self.order.setEnabled(target.value_type != DataType.BOOL)
        with QSignalBlocker(self.offset), QSignalBlocker(self.count):
            self.offset.setMaximum(65536 - words)
            self.count.setMaximum(min(rule.read_limit, 65536 - self.offset.value()) // words)
        operations = f"只读 · 读取 0x{rule.read_code:02X} · 最多读 {rule.read_limit // words} 点"
        if rule.write_codes is not None:
            single, multiple = rule.write_codes
            write_codes = f"0x{single:02X} / 0x{multiple:02X}" if words == 1 else f"0x{multiple:02X}"
            operations = (
                f"可读写 · 读取 0x{rule.read_code:02X} · "
                f"写入 {write_codes} · 最多写 {rule.write_limit // words} 点"
            )
        self.permissions.setText(f"{operations} · 每点占 {words} 个地址 · 偏移从 0 开始")
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
        form.addRow("数据类型", QLabel(target.value_type.value))
        if target.value_type != DataType.BOOL:
            form.addRow("寄存器顺序", QLabel(target.order.label))
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
        try:
            values = tuple(self.target.value_type.parse(field) for field in fields)
            self.target.validate(values)
        except (ClientError, ValueError) as error:
            self.error.setText(str(error))
            return
        self.values = values
        super().accept()
