import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server import ValuesChanged
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.s7.model import Area as S7Area, DataType, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.server_contracts import ListenerState
from all_protocol.storage import ServerStorage, S7ServerStorage
from all_protocol.ui.modbus_server_pane import ModbusServerPane
from all_protocol.ui.s7_server_pane import S7ServerPane
from all_protocol.ui.widgets import EditDialog


class Bridge(QObject):
    event = Signal(object)


@pytest.fixture(params=("modbus", "s7"))
def pane(request, monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    if request.param == "modbus":
        points = tuple(ServerPoint(Area.HR, offset, 0) for offset in (4, 0, 2))
        storage = ServerStorage(tmp_path)
        plan = ServerPlan(stations=(Station(1, points=points),))
        pane_type = ModbusServerPane
    else:
        points = tuple(S7ServerPoint(S7Target(S7Area.DB, 1, offset, DataType.WORD), 0)
                       for offset in (4, 0, 2))
        storage = S7ServerStorage(tmp_path)
        plan = S7ServerPlan(points=points)
        pane_type = S7ServerPane
    storage.save_plan(plan)
    bridge = Bridge()
    widget = pane_type(bridge, storage)
    widget.show()
    app.processEvents()
    yield widget, storage, points
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_sorting_preserves_selection_and_saved_creation_order(pane):
    widget, storage, points = pane
    assert widget.point_sort.currentData() == "created"
    assert widget.points.items == list(points)
    widget.point_table.selectRow(0)
    selected = widget._point
    for mode, expected in (("address_asc", (points[1], points[2], points[0])),
                           ("address_desc", (points[0], points[2], points[1])),
                           ("created", points)):
        widget.point_sort.setCurrentIndex(widget.point_sort.findData(mode))
        assert widget.points.items == list(expected)
        assert widget._point is selected
    # Saving while sorted must retain the original order for the next launch.
    widget.point_sort.setCurrentIndex(widget.point_sort.findData("address_desc"))
    assert widget._save_plan()
    saved = storage.load_plan()
    assert (saved.stations[0].points if isinstance(saved, ServerPlan) else saved.points) == points
    widget._load_points(saved)
    widget.point_sort.setCurrentIndex(widget.point_sort.findData("created"))
    assert widget.points.items == list(points)


def test_sorting_empty_filtered_list_and_deleting_selected_point(pane):
    widget, _, points = pane
    if isinstance(widget, ModbusServerPane):
        original_area = widget.area_tabs.currentIndex()
        widget.area_tabs.setCurrentIndex(widget.area_tabs.count() - 1)
    else:
        original_area = widget.area_filter.currentIndex()
        widget.area_filter.setCurrentIndex(widget.area_filter.findData(S7Area.M.value))
    assert not widget.points.items
    widget.point_sort.setCurrentIndex(widget.point_sort.findData("address_asc"))
    assert widget._point is None
    if isinstance(widget, ModbusServerPane):
        widget.area_tabs.setCurrentIndex(original_area)
    else:
        widget.area_filter.setCurrentIndex(original_area)
    assert widget.points.items == [points[1], points[2], points[0]]
    widget.point_table.selectRow(0)
    widget._delete_point()
    widget.point_sort.setCurrentIndex(widget.point_sort.findData("created"))
    assert widget.points.items == [points[0], points[2]]


def test_adding_and_editing_keep_creation_position(pane, monkeypatch):
    widget, _, _ = pane
    modbus = isinstance(widget, ModbusServerPane)
    new_offset = 6

    def accept(dialog):
        for row in range(dialog.form.rowCount()):
            label = dialog.form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if label and label.widget().text() in ("偏移（0 基）", "字节偏移（0 基）"):
                dialog.form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget().setValue(new_offset)
                break
        else:
            pytest.fail("Missing address field")
        dialog.accept()
        assert not dialog.error.text()

    monkeypatch.setattr(EditDialog, "exec", accept)
    widget._edit_point("add" if modbus else None)
    offsets = lambda: [p.offset if modbus else p.target.offset for p in widget.points.items]
    assert offsets() == [4, 0, 2, 6]
    widget.point_table.selectRow(0)
    new_offset = 8
    widget._edit_point("edit" if modbus else widget._point)
    assert offsets() == [8, 0, 2, 6]
    assert (widget._point.offset if modbus else widget._point.target.offset) == 8


@pytest.mark.parametrize("mode", ("created", "address_asc", "address_desc"))
def test_modbus_live_values_refresh_all_affected_rows(pane, mode):
    widget, _, _ = pane
    if not isinstance(widget, ModbusServerPane):
        pytest.skip("Modbus register notification")
    widget.point_sort.setCurrentIndex(widget.point_sort.findData(mode))
    widget.state = ListenerState.LISTENING
    changed = []
    widget.points.dataChanged.connect(lambda first, last, *args: changed.append((first.row(), last.row())))
    widget._runtime_changed(ValuesChanged(1, Area.HR, 0, (10, 11, 12, 13, 14)))
    assert changed == [(0, 2)]
    assert [widget._current_value(point) for point in widget.points.items] == [
        10 + point.offset for point in widget.points.items
    ]


def test_drag_order_is_saved_and_preserves_point_identity(pane):
    widget, storage, points = pane
    original = widget.points.items[0]
    header = widget.point_table.verticalHeader()
    assert header.sectionsMovable() and not header.isHidden()
    header.moveSection(0, 2)
    expected = (points[1], points[2], points[0])
    assert widget.points.items == list(expected)
    assert widget._point is original
    assert [header.visualIndex(row) for row in range(3)] == [0, 1, 2]
    assert widget._save_plan()
    plan = storage.load_plan()
    assert (plan.stations[0].points if isinstance(plan, ServerPlan) else plan.points) == expected
    widget._load_points(plan)
    header.moveSection(2, 0)
    assert widget.points.items == list(points)
    assert widget._point == points[0]


def test_mouse_drag_on_row_number_changes_definitions(pane):
    widget, _, points = pane
    header = widget.point_table.verticalHeader()
    start = QPoint(header.width() // 2, header.sectionViewportPosition(0) + header.sectionSize(0) // 2)
    end = QPoint(start.x(), header.sectionViewportPosition(2) + header.sectionSize(2) - 3)
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(header.viewport(), start + QPoint(0, 12), delay=20)
    QTest.mouseMove(header.viewport(), end, delay=20)
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert widget.points.items == [points[1], points[2], points[0]]


@pytest.mark.parametrize("mode", ("address_asc", "address_desc", "listening"))
def test_drag_order_is_blocked_while_sorted_or_running(pane, mode):
    widget, _, _ = pane
    if mode == "listening":
        widget.state = ListenerState.LISTENING
        widget._refresh()
    else:
        widget.point_sort.setCurrentIndex(widget.point_sort.findData(mode))
    before = widget._plan()
    header = widget.point_table.verticalHeader()
    assert not header.sectionsMovable()
    header.moveSection(0, 2)
    assert widget._plan() == before
    assert header.visualIndex(0) == 0


def test_drag_filtered_area_keeps_other_points_in_place(pane):
    from dataclasses import replace

    widget, _, points = pane
    if isinstance(widget, ModbusServerPane):
        hidden = ServerPoint(Area.IR, 0, 0)
        station = widget._station
        widget._stations = (replace(station, points=(points[0], hidden, points[1], points[2])),)
    else:
        hidden = S7ServerPoint(S7Target(S7Area.M, 0, 0, DataType.WORD), 0)
        widget._points = (points[0], hidden, points[1], points[2])
        widget.area_filter.setCurrentIndex(widget.area_filter.findData(S7Area.DB.value))
    widget._show_points()
    widget.point_table.verticalHeader().moveSection(2, 0)
    plan = widget._plan()
    assert (plan.stations[0].points if isinstance(plan, ServerPlan) else plan.points) == (
        points[2], hidden, points[0], points[1],
    )


@pytest.mark.parametrize("protocol", ("modbus-rtu", "opcua"))
def test_rtu_and_ua_drag_order_round_trip(protocol, tmp_path, qt_application):
    from all_protocol.adapters.modbus_rtu.server_model import RtuServerPlan
    from all_protocol.adapters.opcua.model import UaTarget, UaType
    from all_protocol.adapters.opcua.server_model import SIMULATOR_NAMESPACE, UaServerPlan, UaServerPoint
    from all_protocol.storage import RtuServerStorage, UaServerStorage
    from all_protocol.ui.modbus_rtu_server_pane import ModbusRtuServerPane
    from all_protocol.ui.opcua_server_pane import UaServerPane

    if protocol == "modbus-rtu":
        points = tuple(ServerPoint(Area.HR, offset, offset) for offset in (4, 0, 2))
        plan = RtuServerPlan(serial_port="COM6", stations=(Station(1, points=points),))
        storage, pane_type = RtuServerStorage(tmp_path), ModbusRtuServerPane
    else:
        points = tuple(UaServerPoint(UaTarget(SIMULATOR_NAMESPACE, "s", str(i), UaType.Int32), i)
                       for i in range(3))
        plan = UaServerPlan(points=points)
        storage, pane_type = UaServerStorage(tmp_path), UaServerPane
    storage.save_plan(plan)
    bridge = Bridge()
    widget = pane_type(bridge, storage)
    try:
        header = widget.point_table.verticalHeader()
        assert header.sectionsMovable()
        header.moveSection(0, 2)
        expected = (points[1], points[2], points[0])
        assert widget.points.items == list(expected)
        assert widget._point == points[0]
        assert widget._save_plan()
        saved = storage.load_plan()
        assert (saved.stations[0].points if protocol == "modbus-rtu" else saved.points) == expected
        widget.state = ListenerState.LISTENING
        widget._refresh()
        assert not header.sectionsMovable()
        header.moveSection(2, 0)
        assert widget.points.items == list(expected)
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
