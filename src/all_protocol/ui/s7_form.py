from dataclasses import asdict
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget,
)

from ..adapters.s7.model import MAX_BYTES, MAX_OFFSET, Area, DataType, S7Config, S7Target
from ..application.contracts import ClientError, Value
from ..storage import ProfileFields
from .widgets import add_field, spinbox


class S7Form:
    def __init__(self):
        self.connection = _ConnectionForm()
        self.query = _QueryForm()

    def connection_config(self) -> tuple[S7Config, str]:
        form = self.connection
        host = form.host.text().strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        config = S7Config(host, form.port.value(), form.rack.value(), form.slot.value(), form.timeout.value())
        config.validate()
        return config, config.endpoint

    def target(self) -> S7Target:
        return self.query.target()

    def can_write(self) -> bool:
        return self.target().writable

    def profile_fields(self) -> ProfileFields:
        config, _ = self.connection_config()
        target = self.target()
        return {
            **asdict(config),
            "area": target.area.name,
            "db": target.db,
            "offset": target.offset,
            "data_type": target.data_type.value,
            "count": target.count,
            "bit": target.bit,
        }

    def restore_profile(self, fields: ProfileFields):
        config = S7Config(fields["host"], fields["port"], fields["rack"], fields["slot"], fields["timeout_ms"])
        target = S7Target(Area[fields["area"]], fields["db"], fields["offset"], DataType(fields["data_type"]),
                          fields["count"], fields["bit"])
        config.validate()
        target.validate()
        form = self.connection
        form.host.setText(config.host)
        form.port.setValue(config.port)
        form.rack.setValue(config.rack)
        form.slot.setValue(config.slot)
        form.timeout.setValue(config.timeout_ms)
        query = self.query
        query.area.setCurrentIndex(query.area.findData(target.area.name))
        query.data_type.setCurrentIndex(query.data_type.findData(target.data_type.value))
        query.offset.setValue(target.offset)
        if target.area == Area.DB:
            query.db.setValue(target.db)
        if target.data_type == DataType.BOOL:
            query.bit.setValue(target.bit)
        query.count.setValue(target.count)

    def confirm_write(self, parent: QWidget, endpoint: str, target: S7Target) -> tuple[Value, ...] | None:
        dialog = _WriteDialog(parent, endpoint, target)
        return dialog.values if dialog.exec() == QDialog.DialogCode.Accepted else None


class _ConnectionForm(QWidget):
    def __init__(self):
        super().__init__()
        self.host = QLineEdit()
        self.host.setPlaceholderText("例如 192.168.0.10")
        self.port = spinbox(1, 65535, 102)
        self.rack = spinbox(0, 7, 0)
        self.slot = spinbox(0, 31, 1)
        self.timeout = spinbox(100, 30000, 3000)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        add_field(layout, "主机", self.host, 1)
        add_field(layout, "端口", self.port)
        add_field(layout, "Rack", self.rack)
        add_field(layout, "Slot", self.slot)
        add_field(layout, "超时（ms）", self.timeout)


class _QueryForm(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.area = QComboBox()
        for area, label in ((Area.DB, "DB 数据块"), (Area.M, "M 标志"),
                            (Area.I, "I 输入"), (Area.Q, "Q 输出")):
            self.area.addItem(label, area.name)
        self.db = spinbox(1, 65535, 1)
        self.offset = spinbox(0, MAX_OFFSET, 0)
        self.bit = spinbox(0, 7, 0)
        self.data_type = QComboBox()
        for data_type in DataType:
            self.data_type.addItem(data_type.value, data_type.value)
        self.data_type.setCurrentIndex(self.data_type.findData(DataType.BYTE.value))
        self.count = spinbox(1, MAX_BYTES, 1)
        self.permissions = QLabel()
        self.permissions.setObjectName("permissions")
        self.permissions.setWordWrap(True)
        fields = QHBoxLayout()
        fields.setSpacing(10)
        add_field(fields, "数据区", self.area, 1)
        add_field(fields, "DB 号", self.db)
        add_field(fields, "字节偏移", self.offset)
        add_field(fields, "数据类型", self.data_type)
        add_field(fields, "位索引", self.bit)
        add_field(fields, "数量", self.count)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(fields)
        layout.addWidget(self.permissions)
        for combo in (self.area, self.data_type):
            combo.currentIndexChanged.connect(self._changed)
        for box in (self.db, self.offset, self.bit, self.count):
            box.valueChanged.connect(self._changed)
        self._changed()

    def target(self) -> S7Target:
        area = Area[self.area.currentData()]
        data_type = DataType(self.data_type.currentData())
        return S7Target(area, self.db.value() if area == Area.DB else 0, self.offset.value(),
                        data_type, self.count.value(), self.bit.value() if data_type == DataType.BOOL else 0)

    def _changed(self):
        area = Area[self.area.currentData()]
        data_type = DataType(self.data_type.currentData())
        bit_access = data_type == DataType.BOOL
        self.db.setEnabled(area == Area.DB)
        self.bit.setEnabled(bit_access)
        blocked = self.offset.blockSignals(True)
        self.offset.setMaximum(MAX_OFFSET - data_type.width + 1)
        self.offset.blockSignals(blocked)
        remaining = MAX_OFFSET + 1 - self.offset.value()
        limit = 1 if bit_access else min(MAX_BYTES, remaining) // data_type.width
        blocked = self.count.blockSignals(True)
        self.count.setMaximum(limit)
        self.count.blockSignals(blocked)
        target = self.target()
        permissions = "可读写" if target.writable else "只读"
        self.permissions.setText(f"{permissions} · {target.label} · 单次最多 {limit} 点")
        self.changed.emit()


class _WriteDialog(QDialog):
    def __init__(self, parent: QWidget, endpoint: str, target: S7Target):
        super().__init__(parent)
        self.setWindowTitle("确认写入")
        self.resize(480, 250)
        self.target = target
        self.values = None
        self.input = QPlainTextEdit()
        instructions = "输入 0 或 1" if target.data_type == DataType.BOOL else "输入数值，用逗号、空格或换行分隔"
        self.input.setPlaceholderText(instructions)
        self.input.setAccessibleName("写入值")
        self.error = QLabel()
        self.error.setObjectName("warning")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        write = buttons.button(QDialogButtonBox.StandardButton.Ok)
        write.setText("确认写入")
        write.setObjectName("write")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout()
        device = QLabel(endpoint)
        device.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("目标设备", device)
        form.addRow("点位范围", QLabel(target.label))
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.input)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.input.setFocus()

    def accept(self):
        fields = re.split(r"[,，\s]+", self.input.toPlainText().strip())
        parse = float if self.target.data_type == DataType.REAL else int
        try:
            values = tuple(parse(field) for field in fields)
            self.target.encode(values)
        except ValueError:
            self.error.setText("请输入有效的十进制数值")
            return
        except ClientError as error:
            self.error.setText(str(error))
            return
        self.values = values
        super().accept()
