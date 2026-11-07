from copy import deepcopy
from dataclasses import replace
import json

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.modbus_tcp.values import DataType, RegisterOrder
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Target
from all_protocol.point_tables import (
    PointDefinition, PointTable, decode_target, export_points_csv, export_table_json,
    import_points_csv, import_table_json, new_id,
)
from all_protocol.project import Project, ProjectFile, ProjectHistory, ProjectWorkspace, decode_project, project_record
from all_protocol.storage import ConnectionProfile, LocalHistory


def profile(protocol):
    if protocol == "modbus-tcp":
        return ConnectionProfile(protocol, "127.0.0.1:1502", {
            "host": "127.0.0.1", "port": 1502, "timeout_ms": 1000, "unit_id": 1, "area": "HR",
            "offset": 0, "count": 1, "data_type": "UInt16", "order": "big",
        }, 1000)
    return ConnectionProfile(protocol, "127.0.0.1:1102 · Rack 0 / Slot 1", {
        "host": "127.0.0.1", "port": 1102, "timeout_ms": 1000, "rack": 0, "slot": 1,
        "area": "DB", "db": 1, "offset": 0, "count": 1, "data_type": "Word", "bit": 0,
    }, 1000)


def example(protocol):
    target = (ModbusTarget(1, Area.HR, 4, 1, DataType.FLOAT32, RegisterOrder.SWAP_WORDS)
              if protocol == "modbus-tcp" else S7Target(S7Area.DB, 3, 4, S7DataType.REAL))
    point = PointDefinition(new_id(), "出口温度", target, "℃", 0.1, -2, '说明含逗号,引号"及\n换行', 2)
    return PointTable(new_id(), "device-1", "运行状态", (point,))


@pytest.mark.parametrize("protocol", ("modbus-tcp", "s7"))
def test_exchange_preserves_targets_metadata_and_renews_import_ids(protocol):
    table = example(protocol)
    table.validate(protocol)
    csv = export_points_csv(table.points)
    assert import_points_csv("\ufeff" + csv, protocol) == table.points
    fresh = import_points_csv(csv, protocol, fresh_ids=True)
    assert fresh[0].id != table.points[0].id
    assert replace(fresh[0], id=table.points[0].id) == table.points[0]
    imported = import_table_json(export_table_json(table, protocol), protocol, "other-device")
    assert imported.id != table.id and imported.device_id == "other-device"
    assert replace(imported.points[0], id=table.points[0].id) == table.points[0]
    assert table.points[0].engineering_text(235) == "21.50"


def test_uint64_engineering_does_not_round_integer_through_float():
    point = PointDefinition("p", "计数", ModbusTarget(1, Area.HR, 0, 1, DataType.UINT64), precision=0)
    assert point.engineering_text(18446744073709551615) == "18446744073709551615"


@pytest.mark.parametrize("changes", [
    {"name": " "}, {"id": ""}, {"unit": 1}, {"description": None},
    {"scale": float("nan")}, {"scale": float("inf")}, {"scale": True},
    {"offset": float("-inf")}, {"precision": True}, {"precision": -1}, {"precision": 13},
    {"target": ModbusTarget(1, Area.HR, 0, 2)}, {"target": ModbusTarget(1, Area.HR, True, 1)},
])
def test_point_definition_rejects_invalid_configuration(changes):
    point = replace(example("modbus-tcp").points[0], **changes)
    with pytest.raises(ValueError):
        point.validate("modbus-tcp")


@pytest.mark.parametrize("csv,match", [
    ("name,area,address_offset\nx,HR,0\n", "表头"),
    ("name,area,address_offset,data_type,unknown\nx,HR,0,UInt16,x\n", "表头"),
    ("name,name,area,address_offset,data_type\nx,x,HR,0,UInt16\n", "表头"),
    ("name,area,address_offset,data_type\nx,HR,-1,UInt16\n", "第 2 行"),
    ("name,area,address_offset,data_type\nx,HR,0,UInt16\ny,HR,65535,Float32\n", "第 3 行"),
    ("name,area,address_offset,data_type,scale\nx,HR,0,UInt16,NaN\n", "第 2 行"),
    ("name,area,address_offset,data_type\nx,HR,0\n", "列数"),
    ("name,area,address_offset,data_type\nx,HR,0,UInt16,extra\n", "列数"),
    ("id,name,area,address_offset,data_type\nsame,x,HR,0,UInt16\nsame,y,HR,1,UInt16\n", "第 3 行"),
    ('name,area,address_offset,data_type\n"unclosed,HR,0,UInt16', "第 2 行"),
])
def test_csv_errors_include_location_and_do_not_return_partial_points(csv, match):
    with pytest.raises(ValueError, match=match):
        import_points_csv(csv, "modbus-tcp")


def test_target_decoder_rejects_wrong_protocol_address_and_count():
    with pytest.raises(ValueError):
        decode_target("s7", {"area": "DB", "db": 0, "offset": 0, "count": 1, "bit": 0, "data_type": "Word"})
    with pytest.raises(ValueError, match="协议"):
        import_table_json(export_table_json(example("modbus-tcp"), "modbus-tcp"), "s7", "device")


def test_project_v1_migrates_in_memory_and_writes_current_version_on_save(tmp_path):
    original = Project("旧项目").remember(profile("modbus-tcp"))
    record = project_record(original)
    record["schema_version"] = 1
    record.pop("point_tables")
    path = tmp_path / "v1.json"
    before = json.dumps(record, ensure_ascii=False).encode("utf-8")
    path.write_bytes(before)
    file = ProjectFile()
    migrated = file.open(path)
    assert migrated == original and migrated.point_tables == ()
    assert path.read_bytes() == before and file.saved == migrated
    table = replace(example("modbus-tcp"), device_id=migrated.devices[0].id)
    project = migrated.with_table(table)
    file.save(project, path)
    assert json.loads(path.read_bytes())["schema_version"] == 3
    assert ProjectFile().open(path) == project


def test_project_rejects_dangling_references_and_duplicate_ids_and_wrong_protocol():
    project = Project("项目").remember(profile("modbus-tcp")).remember(profile("s7"))
    device = next(item for item in project.devices if item.profile.protocol == "modbus-tcp")
    table = replace(example("modbus-tcp"), device_id=device.id)
    project = project.with_table(table)
    duplicate = table.duplicate("副本")
    assert duplicate.points[0].id != table.points[0].id
    assert project.with_table(duplicate).point_tables == (table, duplicate)
    for invalid in (replace(table, device_id="missing"), replace(example("s7"), device_id=device.id),
                    replace(duplicate, points=table.points)):
        with pytest.raises(ValueError):
            project.with_table(invalid)
        assert project.point_tables == (table,)
    record = project_record(project)
    record["point_tables"].append(deepcopy(record["point_tables"][0]))
    with pytest.raises(ValueError, match="重复"):
        decode_project(record)


def test_device_removal_cascades_point_tables_but_keeps_other_devices(tmp_path):
    project = Project("项目").remember(profile("modbus-tcp")).remember(profile("s7"))
    for device in project.devices:
        project = project.with_table(replace(example(device.profile.protocol), device_id=device.id))
    workspace = ProjectWorkspace(project)
    history = ProjectHistory(workspace, LocalHistory(tmp_path))
    assert history.related_table_count(profile("s7")) == 1
    history.forget(profile("s7"))
    assert len(workspace.project.devices) == len(workspace.project.point_tables) == 1
    assert workspace.project.point_tables[0].device_id == workspace.project.devices[0].id


def test_v2_project_migrates_poll_settings_without_touching_source_until_save(tmp_path):
    project = Project("旧点表").remember(profile("modbus-tcp"))
    table = replace(example("modbus-tcp"), device_id=project.devices[0].id)
    record = project_record(project.with_table(table))
    record["schema_version"] = 2
    for item in record["point_tables"]:
        item.pop("interval_ms")
        item.pop("stale_after_ms")
    path = tmp_path / "v2.json"
    original = json.dumps(record).encode()
    path.write_bytes(original)
    file = ProjectFile()
    migrated = file.open(path)
    assert path.read_bytes() == original
    assert migrated.point_tables[0].interval_ms == 1000 and migrated.point_tables[0].stale_after_ms == 3000
    changed = migrated.with_table(replace(migrated.point_tables[0], interval_ms=250, stale_after_ms=1500))
    file.save(changed, path)
    assert json.loads(path.read_bytes())["schema_version"] == 3
    assert ProjectFile().open(path) == changed


@pytest.mark.parametrize("changes", ({"interval_ms": True}, {"interval_ms": 199}, {"interval_ms": 30001},
                                     {"stale_after_ms": False}, {"stale_after_ms": 199}, {"stale_after_ms": 3600001}))
def test_invalid_poll_configuration(changes):
    with pytest.raises(ValueError):
        replace(example("modbus-tcp"), **changes).validate("modbus-tcp")


def test_old_table_json_gets_default_poll_settings_and_new_json_requires_them():
    table = example("modbus-tcp")
    record = json.loads(export_table_json(table, "modbus-tcp"))
    record["table"].pop("interval_ms")
    with pytest.raises(ValueError, match="缺少"):
        import_table_json(json.dumps(record), "modbus-tcp", "device")
    record["schema_version"] = 1
    record["table"].pop("stale_after_ms")
    imported = import_table_json(json.dumps(record), "modbus-tcp", "device")
    assert imported.interval_ms == 1000 and imported.stale_after_ms == 3000
