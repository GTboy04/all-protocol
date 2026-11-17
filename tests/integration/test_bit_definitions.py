from dataclasses import replace
import json

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QTableWidget, QPushButton

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_rtu.model import ModbusRtuTarget
from all_protocol.adapters.modbus_rtu.server_model import RtuServerPlan
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.adapters.opcua.model import UaTarget, UaType
from all_protocol.adapters.opcua.server_model import SIMULATOR_NAMESPACE, UaServerPlan, UaServerPoint
from all_protocol.application.binary import bit_pattern
from all_protocol.bit_definitions import BitDefinition, validate_bit_definitions
from all_protocol.database import decode_project_database
from all_protocol.modbus_points import export_station_points, import_station_points
from all_protocol.point_tables import PointDefinition, PointTable, export_points_csv, import_points_csv, export_table_json, import_table_json
from all_protocol.project import Project, ProjectFile
from all_protocol.storage import ServerStorage, S7ServerStorage, RtuServerStorage, UaServerStorage
from all_protocol.ui.bit_definitions_editor import BitDefinitionsEditor
from all_protocol.ui.bit_inspector import BitInspector, BitReading
from all_protocol.ui.widgets import EditDialog


DEFINITIONS = (BitDefinition(0, "运行", "停止", "运行"),
               BitDefinition(15, '报警,<状态>"', "正常\n无故障", "故障"))


@pytest.mark.parametrize("protocol,target", [
    ("modbus-tcp", ModbusTarget(1, Area.HR, 0, 1, DataType.UINT16)),
    ("modbus-rtu", ModbusRtuTarget(1, Area.HR, 0, 1, DataType.UINT16)),
    ("s7", S7Target(S7Area.DB, 1, 0, S7Type.WORD)),
    ("opcua", UaTarget(SIMULATOR_NAMESPACE, "s", "Status", UaType.UInt16)),
])
def test_named_bit_definitions_exchange_without_text_loss(protocol, target):
    point = PointDefinition("p", "状态字", target, bit_definitions=DEFINITIONS)
    table = PointTable("t", "d", "状态", (point,))
    assert import_points_csv(export_points_csv(table.points), protocol) == table.points
    contents = export_table_json(table, protocol)
    assert json.loads(contents)["schema_version"] == 3
    assert import_table_json(contents, protocol, "new").points[0].bit_definitions == DEFINITIONS
    legacy = json.loads(contents)
    legacy["schema_version"] = 2
    del legacy["table"]["points"][0]["bit_definitions"]
    assert import_table_json(json.dumps(legacy), protocol, "new").points[0].bit_definitions == ()


@pytest.mark.parametrize("definitions,kind", [
    ((BitDefinition(16, "越界"),), "UInt16"),
    ((BitDefinition(-1, "越界"),), "Int32"),
    ((BitDefinition(True, "布尔编号"),), "UInt16"),
    ((BitDefinition(0, "a"), BitDefinition(0, "b")), "UInt16"),
    ((BitDefinition(0, None),), "UInt16"),
    ((BitDefinition(0),), "UInt16"),
    ((BitDefinition(0, "不支持"),), "String"),
])
def test_invalid_definitions_are_rejected(definitions, kind):
    with pytest.raises(ValueError):
        validate_bit_definitions(definitions, kind)


def test_highest_bit_and_bool_bit_are_supported():
    validate_bit_definitions((BitDefinition(63, "最高位"),), "UInt64")
    validate_bit_definitions((BitDefinition(0, "开关"),), "Boolean")


def test_server_definitions_persist_in_local_database_and_project(tmp_path):
    modbus = ServerPlan(stations=(Station(1, points=(ServerPoint(Area.HR, 0, 0, data_type=DataType.UINT16, bit_definitions=DEFINITIONS),)),))
    s7 = S7ServerPlan(points=(S7ServerPoint(S7Target(S7Area.DB, 1, 0, S7Type.WORD), bit_definitions=DEFINITIONS),))
    rtu = RtuServerPlan(serial_port="COM6", stations=modbus.stations)
    ua = UaServerPlan(points=(UaServerPoint(UaTarget(SIMULATOR_NAMESPACE, "s", "Status", UaType.UInt16),
                                          bit_definitions=DEFINITIONS),))
    for storage_type, plan in ((ServerStorage, modbus), (S7ServerStorage, s7), (RtuServerStorage, rtu), (UaServerStorage, ua)):
        storage = storage_type(tmp_path / "local")
        storage.save_plan(plan)
        assert storage.load_plan() == plan
    project = Project("Bit 定义", modbus=modbus, s7=s7, rtu=rtu, ua=ua)
    path = tmp_path / "bits.allprotocol"
    ProjectFile().save(project, path)
    assert decode_project_database(path.read_bytes())["schema_version"] == 7
    assert ProjectFile().open(path) == project
    for csv_format in (False, True):
        assert import_station_points(export_station_points(modbus.stations[0], csv_format=csv_format),
                                     csv_format=csv_format) == modbus.stations[0].points


def test_bit_inspector_refreshes_meanings_and_does_not_invent_unsampled_values(qt_application):
    reading = [BitReading("状态", bit_pattern(1, "UInt16"), "有效", DEFINITIONS, "UInt16")]
    inspector = BitInspector(None, lambda: reading[0])
    try:
        assert inspector.cells[-1].text() == "bit 0\n1"
        assert inspector.cells[0].text() == "bit 15\n0"
        assert "运行" not in inspector.cells[-1].toolTip()
        assert "当前含义：运行" in inspector.detail.text()
        inspector.cells[0].click()
        assert "当前含义：正常\n无故障" in inspector.detail.text()
        reading[0] = replace(reading[0], pattern=bit_pattern(0x8000, "UInt16"))
        inspector.refresh()
        assert "当前含义：故障" in inspector.detail.text()
        assert inspector.cells[-1].text() == "bit 0\n0"
        reading[0] = replace(reading[0], pattern=None)
        inspector.refresh()
        assert len(inspector.cells) == 16
        assert inspector.cells[0].text() == "bit 15\n—"
        assert "暂无采样值" in inspector.detail.text()
        assert "当前含义" not in inspector.detail.text()
        assert "故障" not in inspector.cells[0].text()
    finally:
        inspector.close()


def test_editor_cancel_clear_and_narrower_type_do_not_silently_drop_definitions(qt_application, monkeypatch):
    editor = BitDefinitionsEditor(lambda: "UInt16", DEFINITIONS)

    def cancel(dialog):
        rows = dialog.findChild(QTableWidget)
        rows.item(0, 1).setText("改名")
        dialog.reject()

    monkeypatch.setattr(EditDialog, "exec", cancel)
    editor.click()
    assert editor.definitions == DEFINITIONS

    def save(dialog):
        rows = dialog.findChild(QTableWidget)
        rows.item(0, 2).setText("已停止")
        dialog.accept()
        assert not dialog.error.text()

    monkeypatch.setattr(EditDialog, "exec", save)
    editor.click()
    assert editor.definitions[0].zero == "已停止"
    editor.kind = lambda: "Byte"

    def narrowed(dialog):
        dialog.accept()
        assert "编号" in dialog.error.text()
        dialog.reject()

    monkeypatch.setattr(EditDialog, "exec", narrowed)
    editor.click()
    assert editor.definitions[-1] == DEFINITIONS[-1]

    def clear(dialog):
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "清空所有定义").click()
        dialog.accept()
        assert not dialog.error.text()

    monkeypatch.setattr(EditDialog, "exec", clear)
    editor.click()
    assert editor.definitions == ()
    editor.deleteLater()


@pytest.mark.parametrize("protocol", ("modbus-tcp", "modbus-rtu", "s7", "opcua"))
def test_server_edit_dialog_saves_bit_meanings(protocol, tmp_path, qt_application, monkeypatch):
    from all_protocol.ui.modbus_server_pane import ModbusServerPane
    from all_protocol.ui.modbus_rtu_server_pane import ModbusRtuServerPane
    from all_protocol.ui.s7_server_pane import S7ServerPane
    from all_protocol.ui.opcua_server_pane import UaServerPane

    class Bridge(QObject):
        event = Signal(object)

    if protocol in ("modbus-tcp", "modbus-rtu"):
        station = Station(1, points=(ServerPoint(Area.HR, 0, 0),))
        if protocol == "modbus-tcp":
            storage, plan, pane_type = ServerStorage(tmp_path), ServerPlan(stations=(station,)), ModbusServerPane
        else:
            storage, plan, pane_type = RtuServerStorage(tmp_path), RtuServerPlan(serial_port="COM6", stations=(station,)), ModbusRtuServerPane
    elif protocol == "s7":
        storage, plan, pane_type = S7ServerStorage(tmp_path), S7ServerPlan(), S7ServerPane
    else:
        point = UaServerPoint(UaTarget(SIMULATOR_NAMESPACE, "s", "Status", UaType.UInt16))
        storage, plan, pane_type = UaServerStorage(tmp_path), UaServerPlan(points=(point,)), UaServerPane
    storage.save_plan(plan)
    bridge = Bridge()
    pane = pane_type(bridge, storage)

    def accept(dialog):
        rows = dialog.findChild(QTableWidget)
        if rows is not None:
            for item in DEFINITIONS:
                for column, text in enumerate((item.name, item.zero, item.one), 1):
                    rows.item(item.bit, column).setText(text)
        else:
            dialog.findChild(BitDefinitionsEditor).click()
        dialog.accept()
        assert not dialog.error.text()
        return dialog.result()

    monkeypatch.setattr(EditDialog, "exec", accept)
    try:
        pane.point_table.selectRow(0)
        pane._edit_point("edit" if isinstance(pane, ModbusServerPane) else pane._point)
        assert pane.points.items[0].bit_definitions == DEFINITIONS
        assert pane._bit_reading().definitions == DEFINITIONS
        assert pane._save_plan()
        saved = storage.load_plan()
        saved_points = saved.stations[0].points if protocol.startswith("modbus") else saved.points
        assert saved_points[0].bit_definitions == DEFINITIONS
    finally:
        pane.close()
        pane.deleteLater()
