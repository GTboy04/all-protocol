from dataclasses import asdict
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget
from serial.tools.list_ports import comports
from ..adapters.modbus_rtu.model import ModbusRtuConfig, ModbusRtuTarget
from ..point_tables import decode_target, target_record
from .modbus_tcp_form import ModbusTcpForm, _QueryForm
from .widgets import add_field, spinbox


class SerialConnectionForm(QWidget):
    def __init__(self):
        super().__init__()
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.addItems(sorted({port.device for port in comports()}))
        self.serial_port.setCurrentIndex(-1)
        self.serial_port.lineEdit().setPlaceholderText("例如 COM3")
        self.baudrate = spinbox(300, 4000000, 9600)
        self.parity = QComboBox()
        for value, label in (("E", "偶校验 E"), ("N", "无校验 N"), ("O", "奇校验 O")):
            self.parity.addItem(label, value)
        self.stopbits = spinbox(1, 2, 1)
        self.timeout = spinbox(100, 30000, 3000)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, widget in (("串口（8 数据位）", self.serial_port), ("波特率", self.baudrate),
                              ("校验", self.parity), ("停止位", self.stopbits), ("超时（ms）", self.timeout)):
            add_field(layout, label, widget)

    def config(self):
        config = ModbusRtuConfig(self.serial_port.currentText().strip(), self.baudrate.value(),
                                 self.parity.currentData(), self.stopbits.value(), self.timeout.value())
        config.validate()
        return config

    def restore(self, config):
        self.serial_port.setCurrentText(config.serial_port)
        self.baudrate.setValue(config.baudrate)
        self.parity.setCurrentIndex(self.parity.findData(config.parity))
        self.stopbits.setValue(config.stopbits)
        self.timeout.setValue(config.timeout_ms)


class RtuQueryForm(_QueryForm):
    def __init__(self):
        super().__init__()
        self.unit.setMaximum(247)
        self.unit.setToolTip("RTU 单播站号 1–247；超时或无效应答后暂停该站号，检查链路后重新连接。")

    def target(self):
        return ModbusRtuTarget(**asdict(super().target()))


class ModbusRtuForm(ModbusTcpForm):
    def __init__(self):
        self.connection = SerialConnectionForm()
        self.query = RtuQueryForm()

    def connection_config(self):
        config = self.connection.config()
        return config, config.endpoint

    def profile_fields(self):
        config, _ = self.connection_config()
        target = self.target()
        target.validate()
        return dict(asdict(config), serial_port=config.endpoint, **target_record(target))

    def restore_profile(self, fields):
        from ..project import validate_profile
        from ..storage import ConnectionProfile
        config = ModbusRtuConfig(*(fields[key] for key in ("serial_port", "baudrate", "parity", "stopbits", "timeout_ms")))
        validate_profile(ConnectionProfile("modbus-rtu", config.endpoint, fields, 1000))
        target = decode_target("modbus-rtu", fields, single=False)
        self.connection.restore(config)
        self.query.unit.setValue(target.unit_id)
        self.query.area.setCurrentIndex(self.query.area.findData(target.area.value))
        self.query.data_type.setCurrentIndex(self.query.data_type.findData(target.value_type.value))
        self.query.order.setCurrentIndex(self.query.order.findData(target.order.value))
        self.query.offset.setValue(target.offset)
        self.query.count.setValue(target.count)
