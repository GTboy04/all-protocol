from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
from all_protocol.database import decode_project_database, DATABASE_NAME

import pytest

from all_protocol import project as module
from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType, RegisterOrder
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.project import (
    Project, ProjectFile, ProjectHistory, ProjectServerStorage, ProjectWorkspace, WorkspaceLayout,
    decode_project, project_record,
)
from all_protocol.storage import ConnectionProfile, LocalHistory, ServerStorage, S7ServerStorage


def modbus_profile(host="192.0.2.1"):
    return ConnectionProfile("modbus-tcp", f"{host}:502", {
        "host": host, "port": 502, "timeout_ms": 3000, "unit_id": 7, "area": "HR",
        "offset": 10, "count": 2, "data_type": "Float32", "order": "swap_words",
    }, 750)


def s7_profile():
    return ConnectionProfile("s7", "192.0.2.2:102 · Rack 0 / Slot 1", {
        "host": "192.0.2.2", "port": 102, "timeout_ms": 1000, "rack": 0, "slot": 1,
        "area": "DB", "db": 3, "offset": 4, "count": 1, "data_type": "Real", "bit": 0,
    }, 1500)


def sample_project():
    return Project(
        "生产线联调", modbus=ServerPlan(stations=(Station(7, "温控器", (
            ServerPoint(Area.HR, 10, 24.5, "温度", DataType.FLOAT32, RegisterOrder.SWAP_WORDS),
        )),)),
        s7=S7ServerPlan(points=(S7ServerPoint(S7Target(S7Area.DB, 3, 4, S7DataType.REAL), 12.5, "速度"),)),
    ).remember(modbus_profile()).remember(s7_profile())


def test_project_round_trip_is_portable_and_keeps_stable_ids(tmp_path):
    original = sample_project()
    file = ProjectFile()
    path = tmp_path / "中文项目.allprotocol.json"
    file.save(original, path)
    assert ProjectFile().open(path) == original
    assert file.saved == original
    updated = original.remember(replace(modbus_profile(), interval_ms=900))
    assert updated.devices[0].id == original.devices[1].id
    assert updated.devices[0].profile.interval_ms == 900
    assert original.devices[1].profile.interval_ms == 750
    record = decode_project_database(path.read_bytes())
    assert set(record) == {"format", "schema_version", "name", "devices", "servers", "point_tables"}
    assert path.read_bytes().startswith(b"SQLite format 3")
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("SELECT count(*) FROM journal_entries").fetchone()[0] == 0


def test_optional_layout_round_trips_and_damaged_layout_leaves_protocol_configuration_usable(tmp_path):
    project = sample_project()
    layout = WorkspaceLayout(315, 440, False, ("server", "s7"), True, False)
    path = tmp_path / "layout.json"
    ProjectFile().save(replace(project, layout=layout), path)
    assert ProjectFile().open(path) == replace(project, layout=layout)
    record = decode_project_database(path.read_bytes())
    assert record["schema_version"] == 3 and record["workspace_layout"]["version"] == 1
    for field, value in (("version", 99), ("navigation", -1), ("journal", True),
                         ("selection", ["unknown", "s7"]), ("journal_visible", 1)):
        invalid = deepcopy(record)
        invalid["workspace_layout"][field] = value
        path.write_text(json.dumps(invalid), encoding="utf-8")
        assert ProjectFile().open(path) == project


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(schema_version=True),
    lambda data: data.update(schema_version=9),
    lambda data: data.update(format="other"),
    lambda data: data.update(name=" "),
    lambda data: data.update(devices={}),
    lambda data: data["devices"].append(deepcopy(data["devices"][0])),
    lambda data: data["devices"][0].update(id=""),
    lambda data: data["devices"][0]["profile"].update(protocol="unknown"),
    lambda data: data["devices"][0]["profile"].update(interval_ms=True),
    lambda data: data["devices"][0]["profile"].update(endpoint="different"),
    lambda data: data["devices"][0]["profile"]["fields"].update(port=True),
    lambda data: data["devices"][0]["profile"]["fields"].update(offset=-1),
    lambda data: data["devices"][0]["profile"]["fields"].pop("host"),
    lambda data: data["servers"].update(unknown={}),
    lambda data: data["servers"]["s7"].update(points=[None]),
    lambda data: data["servers"]["modbus-tcp"].update(stations=None),
])
def test_invalid_project_rejected_as_a_whole(mutation):
    record = json.loads(json.dumps(project_record(sample_project())))
    mutation(record)
    with pytest.raises(ValueError):
        decode_project(record)


def test_invalid_open_keeps_previous_file_and_revision(tmp_path, monkeypatch):
    file = ProjectFile()
    valid, invalid = tmp_path / "valid.json", tmp_path / "invalid.json"
    project = sample_project()
    file.save(project, valid)
    for content in (b"{", b"[]", b"\xff", b'{"value": NaN}'):
        invalid.write_bytes(content)
        with pytest.raises(ValueError):
            file.open(invalid)
        assert file.path == valid and file.saved == project
    monkeypatch.setattr(module, "PROJECT_LIMIT_BYTES", 5)
    invalid.write_bytes(b"123456")
    with pytest.raises(ValueError, match="10 MiB"):
        file.open(invalid)


def test_failed_save_and_external_modification_do_not_change_baseline(tmp_path, monkeypatch):
    first, second = ProjectFile(), ProjectFile()
    path = tmp_path / "project.json"
    original = sample_project()
    first.save(original, path)
    second.open(path)
    second.save(replace(original, name="外部修改"), path)
    external = path.read_bytes()
    with pytest.raises(ValueError, match="其他程序"):
        first.save(replace(original, name="本机修改"), path)
    assert path.read_bytes() == external and first.saved == original
    destination = tmp_path / "copy.json"

    def fail(*_args):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(module, "_replace_file", fail)
    with pytest.raises(OSError):
        first.save(original, destination)
    assert first.path == path and not destination.exists()
    assert first.saved == original


def test_deleted_project_not_silently_recreated(tmp_path):
    file = ProjectFile()
    path = tmp_path / "project.json"
    file.save(Project("项目"), path)
    path.unlink()
    with pytest.raises(ValueError, match="删除"):
        file.save(Project("修改"), path)


def test_import_copies_configuration_without_changing_local_files(tmp_path):
    original = sample_project()
    history, modbus, s7 = LocalHistory(tmp_path), ServerStorage(tmp_path), S7ServerStorage(tmp_path)
    for item in reversed(original.devices):
        history.remember(item.profile)
    modbus.save_plan(original.modbus)
    s7.save_plan(original.s7)
    paths = (history.database_path,)
    before = [path.read_bytes() for path in paths]
    workspace = ProjectWorkspace.from_local("导入项目", tmp_path)
    assert tuple(item.profile for item in workspace.project.devices) == history.profiles()
    assert workspace.project.modbus == original.modbus and workspace.project.s7 == original.s7
    profiles = ProjectHistory(workspace, history)
    profiles.remember(modbus_profile("192.0.2.99"))
    profiles.forget(s7_profile())
    ProjectServerStorage(workspace, "modbus", modbus).save_plan(ServerPlan(port=2502))
    assert [path.read_bytes() for path in paths] == before
    assert workspace.project.modbus.port == 2502
    assert len(workspace.project.devices) == 2


def test_invalid_local_history_not_silently_migrated(tmp_path):
    path = tmp_path / "connections.json"
    for contents in ('{', '[{}]', '[{"protocol":"unknown"}]'):
        path.write_text(contents, encoding="utf-8")
        with pytest.raises(ValueError):
            ProjectWorkspace.from_local("项目", tmp_path)
        assert path.read_text(encoding="utf-8") == contents


def test_failed_atomic_commit_preserves_existing_project(tmp_path, monkeypatch):
    from all_protocol import storage

    file = ProjectFile()
    path = tmp_path / "project.json"
    file.save(Project("原项目"), path)
    before = path.read_bytes()

    class FailingSaveFile:
        def __init__(self, _path):
            pass

        def open(self, _mode):
            return True

        def write(self, contents):
            return len(contents)

        def commit(self):
            return False

        def errorString(self):
            return "模拟提交失败"

    monkeypatch.setattr(storage, "QSaveFile", FailingSaveFile)
    with pytest.raises(OSError, match="提交失败"):
        file.save(Project("修改"), path)
    assert path.read_bytes() == before
    assert file.saved.name == "原项目"


def test_invalid_sqlite_and_foreign_database_keep_previous_project(tmp_path):
    file = ProjectFile()
    valid, invalid = tmp_path / "valid.allprotocol", tmp_path / "invalid.allprotocol"
    file.save(Project("original"), valid)
    with closing(sqlite3.connect(invalid)) as connection, connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = invalid.read_bytes()
    with pytest.raises(ValueError):
        file.open(invalid)
    assert invalid.read_bytes() == before
    assert file.saved.name == "original" and file.path == valid
    invalid.write_bytes(b"SQLite format 3\x00" + b"bad" * 100)
    with pytest.raises(ValueError):
        file.open(invalid)
    assert file.path == valid
