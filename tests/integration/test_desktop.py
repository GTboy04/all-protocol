from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from all_protocol.application.contracts import Point, ProtocolRegistration
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import DesktopProtocol, MainWindow


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
