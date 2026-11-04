from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.application.contracts import ConnectionState, Point, ProtocolRegistration
from all_protocol.storage import LocalHistory
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import DesktopProtocol, MainWindow
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm


@dataclass(frozen=True)
class NamedPoint:
    label: str


class ReadOnlyClient:
    def __init__(self, config, _events):
        assert config == "demo://local"

    async def connect(self):
        pass

    async def close(self):
        pass

    def function(self, _command):
        return "ReadValue"

    async def read(self, target):
        return (Point(target.label, "温度", 24.5),)


class QueryWidget(QWidget):
    changed = Signal()


class NamedPointForm:
    def __init__(self):
        self.connection = QWidget()
        self.query = QueryWidget()

    def connection_config(self):
        return "demo://local", "demo://local"

    def target(self):
        return NamedPoint("sensor.temperature")


def wait_for(condition):
    deadline = monotonic() + 3
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def test_window_can_run_a_named_point_protocol(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    bridge = IoBridge()
    protocol = DesktopProtocol(ProtocolRegistration("named", "Named point", ReadOnlyClient), NamedPointForm)
    window = MainWindow(bridge, (protocol,))
    try:
        window.show()
        window.connection_button.click()
        wait_for(window.read_button.isEnabled)
        assert not window.write_button.isEnabled()
        window.read_button.click()
        wait_for(lambda: window.results.rowCount() == 1 and window.read_button.isEnabled())
        assert window.results.items[0].address == "sensor.temperature"
        assert window.results.items[0].value == 24.5
        assert window.journal.items[0].function == "ReadValue"
        window.interval.setValue(200)
        window.poll_button.click()
        wait_for(lambda: window.journal.rowCount() >= 4)
        window.poll_button.click()
        wait_for(window.read_button.isEnabled)
        window.close()
        wait_for(lambda: not window.isVisible())
        assert window.journal.items[0].operation == "断开"
    finally:
        bridge.join()
        window.close()
        app.processEvents()


def test_window_restores_devices_and_packet_log_without_connecting(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    connections = []

    class Client:
        def __init__(self, config, events):
            self.config = config
            self.events = events

        async def connect(self):
            connections.append(self.config)

        async def close(self):
            pass

        def function(self, command):
            return f"0x{command.target.validate():02X}"

        async def read(self, target):
            self.events.packet(True, b"\x04\x00\x2a\x00\x01")
            self.events.packet(False, b"\x04\x02\x00\x64")
            return (Point(str(target.offset), "30043", 100),)

    protocol = DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", Client), ModbusTcpForm)
    bridge = IoBridge()
    window = MainWindow(bridge, (protocol,), LocalHistory(tmp_path))
    try:
        window.show()
        form = window._form
        form.connection.host.setText("192.0.2.1")
        window.connection_button.click()
        wait_for(window.read_button.isEnabled)
        assert not window.recent_connections.isEnabled()
        window.connection_button.click()
        wait_for(lambda: window.recent_connections.isEnabled())

        form.connection.host.setText("192.0.2.2")
        form.connection.port.setValue(1502)
        form.connection.timeout.setValue(2400)
        window.connection_button.click()
        wait_for(window.read_button.isEnabled)
        form.query.unit.setValue(7)
        form.query.area.setCurrentIndex(form.query.area.findData(Area.IR.value))
        form.query.offset.setValue(42)
        form.query.count.setValue(1)
        window.interval.setValue(750)
        window.read_button.click()
        wait_for(lambda: window.results.rowCount() == 1)
        window.close()
        wait_for(lambda: not window.isVisible())
    finally:
        bridge.join()
        app.processEvents()

    bridge = IoBridge()
    window = MainWindow(bridge, (protocol,), LocalHistory(tmp_path))
    try:
        window.show()
        assert window._state.connection == ConnectionState.OFFLINE
        assert len(connections) == 2
        assert window._form.connection_config()[0] == ModbusTcpConfig("192.0.2.2", 1502, 2400)
        assert window._form.target() == ModbusTarget(7, Area.IR, 42, 1)
        assert window.interval.value() == 750
        assert window.recent_connections.count() == 2

        read_row = next(index for index, entry in enumerate(window.journal.items) if entry.operation == "读取")
        window.log_table.selectRow(read_row)
        detail = window.detail.toPlainText()
        assert "Modbus TCP  192.0.2.2:1502" in detail
        assert "TX  04 00 2A 00 01" in detail and "RX  04 02 00 64" in detail

        window.recent_connections.activated.emit(1)
        assert window._form.connection_config()[0] == ModbusTcpConfig("192.0.2.1")
        assert len(connections) == 2
        window.close()
        wait_for(lambda: not window.isVisible())
    finally:
        bridge.join()
        app.processEvents()
