from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox, QWidget

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.s7.model import Area as S7Area
from all_protocol.adapters.s7.model import DataType, S7Config, S7Target
from all_protocol.application.contracts import ConnectionState, Point, ProtocolRegistration
from all_protocol.storage import LocalHistory
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import DesktopProtocol, MainWindow
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.s7_form import S7Form


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


def test_unwritable_local_history_does_not_block_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    class Client:
        def __init__(self, *_args):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    history = LocalHistory(tmp_path)

    def fail(_profile):
        raise OSError("模拟历史文件不可写")

    monkeypatch.setattr(history, "remember", fail)
    bridge = IoBridge()
    window = MainWindow(bridge, (DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus", Client), ModbusTcpForm),), history)
    try:
        window._form.connection.host.setText("127.0.0.1")
        window.show()
        window.connection_button.click()
        wait_for(lambda: window._state.connection == ConnectionState.ONLINE)
        assert "无法保存最近连接" in window.statusBar().currentMessage()
    finally:
        bridge.join()
        window.close()
        app.processEvents()


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


def test_s7_workbench_writes_typed_value_and_restores_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    config = S7Config("192.0.2.1", rack=1, slot=3)
    written = []

    class TestClient:
        def __init__(self, actual_config, events):
            assert actual_config == config

        async def connect(self):
            pass

        async def close(self):
            pass

        def function(self, command):
            return "Write Var (0x05)" if command.values is not None else "Read Var (0x04)"

        async def read(self, target):
            return target.decode(bytes.fromhex("FF 85"))

        async def write(self, target, values):
            written.append((target, target.encode(values)))

    protocols = (
        DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", TestClient, True), ModbusTcpForm),
        DesktopProtocol(ProtocolRegistration("s7", "Siemens S7", TestClient, True), S7Form),
    )
    history = LocalHistory(tmp_path)
    bridge = IoBridge()
    window = MainWindow(bridge, protocols, history)
    try:
        window.show()
        window.protocol.setCurrentIndex(window.protocol.findData("s7"))
        form = window._form
        form.connection.host.setText(config.host)
        form.connection.rack.setValue(config.rack)
        form.connection.slot.setValue(config.slot)
        window.connection_button.click()
        wait_for(window.read_button.isEnabled)
        assert not window.protocol.isEnabled()
        query = form.query
        query.area.setCurrentIndex(query.area.findData("I"))
        assert not window.write_button.isEnabled() and not query.db.isEnabled()
        query.area.setCurrentIndex(query.area.findData("DB"))
        query.db.setValue(10)
        query.offset.setValue(4)
        query.data_type.setCurrentIndex(query.data_type.findData("Int"))
        target = S7Target(S7Area.DB, 10, 4, DataType.INT)
        assert form.target() == target and window.write_button.isEnabled()

        def confirm():
            dialog = app.activeModalWidget()
            dialog.input.setPlainText("-123")
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, confirm)
        window.write_button.click()
        wait_for(lambda: written and window.read_button.isEnabled())
        assert written == [(target, b"\xFF\x85")]
        window.interval.setValue(200)
        window.poll_button.click()
        wait_for(lambda: window.results.rowCount() == 1 and window.journal.rowCount() >= 4)
        window.poll_button.click()
        wait_for(window.read_button.isEnabled)
        assert window.results.items[0].value == -123

        saved, = history.profiles()
        restored = S7Form()
        restored.restore_profile(saved.fields)
        assert saved.protocol == "s7" and saved.endpoint == config.endpoint and saved.interval_ms == 200
        assert restored.connection_config() == (config, config.endpoint)
        assert restored.target() == target
        window.close()
        wait_for(lambda: not window.isVisible())
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
        assert not window.delete_recent_button.isEnabled()
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

        def choose(role):
            dialog = app.activeModalWidget()
            next(button for button in dialog.buttons() if dialog.buttonRole(button) == role).click()

        history = LocalHistory(tmp_path)
        profiles, entries = history.profiles(), history.recent_entries()
        QTimer.singleShot(0, lambda: choose(QMessageBox.ButtonRole.RejectRole))
        window.delete_recent_button.click()
        assert history.profiles() == profiles

        QTimer.singleShot(0, lambda: choose(QMessageBox.ButtonRole.DestructiveRole))
        window.delete_recent_button.click()
        assert window.recent_connections.count() == 1
        assert not window.delete_recent_button.isEnabled()
        assert LocalHistory(tmp_path).profiles() == profiles[:1]
        assert history.recent_entries() == entries
        assert window._form.connection_config()[0] == ModbusTcpConfig("192.0.2.1")
        window.close()
        wait_for(lambda: not window.isVisible())
    finally:
        bridge.join()
        app.processEvents()
