from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QMessageBox, QSpinBox
import pytest

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.application.server_contracts import ListenerState
from all_protocol.modbus_points import export_station_points, import_station_points
from all_protocol.storage import ServerStorage
from all_protocol.ui.modbus_server_pane import ModbusServerPane
from all_protocol.ui.widgets import EditDialog


class Bridge(QObject):
    event = Signal(object)


@pytest.fixture
def pane(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    storage = ServerStorage(tmp_path)
    storage.save_plan(ServerPlan(stations=(Station(7, "源站", (ServerPoint(Area.HR, 0, 42),)),)))
    bridge = Bridge()
    widget = ModbusServerPane(bridge, storage)
    widget.show()
    app.processEvents()
    yield widget, storage
    widget.close()
    widget.deleteLater()


def test_copy_dialog_rejects_existing_id_then_saves_and_reloads(pane):
    widget, storage = pane

    def accept():
        dialog = widget.findChild(EditDialog)
        unit = dialog.findChild(QSpinBox)
        unit.setValue(7)
        dialog.accept()
        assert "已存在" in dialog.error.text()
        assert len(widget._stations) == 1
        unit.setValue(255)
        dialog.findChild(QLineEdit).setText("新站")
        dialog.accept()

    QTimer.singleShot(0, accept)
    widget.copy_station.click()
    assert widget._station.unit_id == 255
    assert widget._station.points == widget._stations[0].points
    assert widget._save_plan()
    assert storage.load_plan() == widget._plan()


@pytest.mark.parametrize("suffix", ("json", "csv"))
def test_export_import_conflict_cancel_and_persistence(pane, monkeypatch, tmp_path, suffix):
    widget, storage = pane
    path = tmp_path / f"points.{suffix}"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(path), ""))
    widget.export_points.click()
    points = import_station_points(path.read_text(encoding="utf-8-sig"), csv_format=suffix == "csv")
    assert len(points) == 1 and points[0].initial == 42
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    widget.import_points.click()
    assert "已存在" in widget.feedback.text()
    assert len(widget._station.points) == 1
    source = Station(20, points=(ServerPoint(Area.IR, 10, 123), ServerPoint(Area.DI, 5, 1)))
    path.write_text(export_station_points(source, csv_format=suffix == "csv"), encoding="utf-8-sig")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    widget.import_points.click()
    assert len(widget._station.points) == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    widget.import_points.click()
    assert len(widget._station.points) == 3
    assert widget._station.unit_id == 7
    assert widget._save_plan()
    assert storage.load_plan() == widget._plan()


def test_actions_follow_listener_and_selection_state(pane):
    widget, _ = pane
    for state in (ListenerState.STARTING, ListenerState.LISTENING, ListenerState.STOPPING):
        widget.state = state
        widget._refresh()
        assert not widget.copy_station.isEnabled()
        assert not widget.import_points.isEnabled()
        assert widget.export_points.isEnabled()
    widget.state = ListenerState.STOPPED
    widget._stations = ()
    widget._show_stations(0)
    assert not widget.copy_station.isEnabled()
    assert not widget.import_points.isEnabled()
    assert not widget.export_points.isEnabled()
