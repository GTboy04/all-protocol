from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
import sqlite3

import pytest

from all_protocol.database import DATABASE_NAME

from all_protocol import storage
from all_protocol.application.contracts import ErrorKind, JournalEntry
from all_protocol.storage import ConnectionProfile, LocalHistory


def test_old_journal_without_operation_id_remains_readable(tmp_path):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime(2026, 11, 7), "读取", "point", "read", 1, "成功", operation_id=7)
    history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        record = json.loads(connection.execute("SELECT payload FROM journal_entries").fetchone()[0])
    record.pop("operation_id")
    history.database_path.unlink()
    history.journal_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert history.recent_entries() == (replace(entry, operation_id=None),)


def test_device_session_metadata_round_trips_and_r3_journal_stays_readable(tmp_path):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime(2026, 11, 7), "读取", "point", "read", 1, "成功",
                         device_id="device-a", session_id="connection-uuid", generation=3)
    history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        record = json.loads(connection.execute("SELECT payload FROM journal_entries").fetchone()[0])
    for name in ("device_id", "session_id", "generation"):
        record.pop(name)
    history.database_path.unlink()
    history.journal_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert history.recent_entries() == (replace(entry, device_id="", session_id="", generation=0),)


def test_profiles_merge_by_protocol_and_endpoint(tmp_path):
    modbus = ConnectionProfile("modbus-tcp", "192.0.2.1:502", {"timeout_ms": 1000}, 200)
    other_protocol = replace(modbus, protocol="other")
    updated = replace(modbus, fields={"timeout_ms": 5000})
    LocalHistory(tmp_path).remember(modbus)
    LocalHistory(tmp_path).remember(other_protocol)
    LocalHistory(tmp_path).remember(updated)
    assert LocalHistory(tmp_path).profiles() == (updated, other_protocol)
    assert LocalHistory(tmp_path).forget(modbus) == (other_protocol,)
    assert LocalHistory(tmp_path).forget(modbus) == (other_protocol,)
    assert LocalHistory(tmp_path).forget(other_protocol) == ()
    assert LocalHistory(tmp_path).profiles() == ()


def test_journal_clears_on_overflow_without_removing_profiles(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    profile = ConnectionProfile("modbus-tcp", "192.0.2.1:502", {"host": "192.0.2.1"}, 1000)
    history.remember(profile)
    entry = JournalEntry(
        1, datetime(2026, 11, 4, 10, 30), "读取", "站 1 · HR · 0", "0x03", 3000,
        "请求超时", ErrorKind.TIMEOUT, "设备没有应答", ((True, b"\x00\xff"),),
        profile.protocol, profile.endpoint,
    )
    assert not history.append_entry(entry)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        limit = connection.execute("SELECT sum(size_bytes) FROM journal_entries").fetchone()[0] * 2
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", limit)
    second, third = replace(entry, request_id=2), replace(entry, request_id=3)
    assert not history.append_entry(second)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        assert connection.execute("SELECT sum(size_bytes) FROM journal_entries").fetchone()[0] == limit
    assert LocalHistory(tmp_path).recent_entries() == (second, entry)
    assert history.append_entry(third)
    assert LocalHistory(tmp_path).recent_entries() == (third,)
    assert LocalHistory(tmp_path).profiles() == (profile,)


def test_journal_recovers_after_partial_write_and_cleans_oversize_on_start(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime.now(), "读取", "站 1", "0x03", 1, "成功")
    tmp_path.mkdir(exist_ok=True)
    history.journal_path.write_bytes(b'{"request_id":')
    assert not history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", 1)
    assert history.recent_entries() == ()
    assert history.journal_path.read_bytes() == b'{"request_id":'
    assert history.read_log().retention.rotated_at is not None


def test_journal_retention_survives_rotation_and_reopen(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    first = JournalEntry(1, datetime.now(), "读取", "站 1", "0x03", 1, "成功", detail="x" * 200)
    history.append_entry(first)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        size = connection.execute("SELECT sum(size_bytes) FROM journal_entries").fetchone()[0]
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", 2 * size - 1)
    second = replace(first, request_id=2)
    assert history.append_entry(second)
    retained = LocalHistory(tmp_path).read_log()
    assert retained.entries == (second,) and retained.retention.rotated_at.utcoffset() is not None
    assert not retained.retention.clipped and not retained.retention.incomplete
    third = replace(first, request_id=3, detail="")
    assert not history.append_entry(third)
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        connection.execute("UPDATE journal_state SET incomplete=1 WHERE stream='client'")
    monkeypatch.setattr(storage, "VISIBLE_LOG_ROWS", 1)
    reopened = LocalHistory(tmp_path).read_log()
    assert reopened.entries == (third,)
    assert reopened.retention.rotated_at == retained.retention.rotated_at
    assert reopened.retention.clipped and reopened.retention.incomplete


def test_legacy_profiles_migrate_once_and_originals_survive(tmp_path):
    from dataclasses import asdict

    history = LocalHistory(tmp_path)
    first = ConnectionProfile("modbus-tcp", "host:502", {"host": "host"}, 1000)
    second = replace(first, endpoint="other:502")
    original = json.dumps([asdict(first), asdict(second)]).encode()
    history.profiles_path.write_bytes(original)
    assert history.profiles() == (first, second)
    history.forget(first)
    assert LocalHistory(tmp_path).profiles() == (second,)
    assert history.profiles_path.read_bytes() == original
    assert not history.journal_path.exists()


def test_concurrent_history_writers_do_not_lose_profiles_or_logs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def write(index):
        history = LocalHistory(tmp_path)
        history.remember(ConnectionProfile("s7", str(index), {}, 1000))
        history.append_entry(JournalEntry(index, datetime.now(), "read", "point", "read", 1, "ok"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    assert len(LocalHistory(tmp_path).profiles()) == 20
    assert {entry.request_id for entry in LocalHistory(tmp_path).recent_entries()} == set(range(20))


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path):
    history = LocalHistory(tmp_path)
    history.profiles_path.write_text('[{}]', encoding="utf-8")
    with pytest.raises(ValueError):
        history.profiles()
    history.profiles_path.write_text('[]', encoding="utf-8")
    assert history.profiles() == ()


def test_unknown_database_version_is_preserved(tmp_path):
    history = LocalHistory(tmp_path)
    history.profiles()
    with closing(sqlite3.connect(history.database_path)) as connection, connection:
        connection.execute("PRAGMA user_version=999")
    before = history.database_path.read_bytes()
    with pytest.raises(ValueError, match="版本"):
        history.profiles()
    assert history.database_path.read_bytes() == before


def test_oversize_entry_does_not_clear_previous_log(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime.now(), "read", "point", "read", 1, "ok")
    history.append_entry(entry)
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", 500)
    with pytest.raises(OSError):
        history.append_entry(replace(entry, detail="x" * 1000))
    assert history.recent_entries() == (entry,)


def test_plan_update_rolls_back_all_rows_when_write_fails(tmp_path, monkeypatch):
    from all_protocol.storage import ServerStorage, S7ServerStorage
    from all_protocol.adapters.modbus_tcp.server_model import ServerPlan

    modbus, s7 = ServerStorage(tmp_path), S7ServerStorage(tmp_path)
    original, other = modbus.load_plan(), s7.load_plan()
    write_plan = storage.write_plan

    def fail_after_write(connection, record):
        write_plan(connection, record)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage, "write_plan", fail_after_write)
    with pytest.raises(OSError):
        modbus.save_plan(ServerPlan(port=2502))
    assert modbus.load_plan() == original
    assert s7.load_plan() == other


def test_rotation_only_clears_its_own_stream(tmp_path, monkeypatch):
    from all_protocol.application.server_contracts import ServerEvent, ServerLifecycle, LifecycleAction
    from all_protocol.storage import ServerStorage

    history, server = LocalHistory(tmp_path), ServerStorage(tmp_path)
    event = ServerEvent("modbus-tcp", "localhost:1502", datetime.now(),
                        ServerLifecycle(LifecycleAction.STARTED, "started"))
    server.append_event(event)
    first = JournalEntry(1, datetime.now(), "read", "point", "read", 1, "ok")
    history.append_entry(first)
    with closing(sqlite3.connect(history.database_path)) as connection:
        size = connection.execute("SELECT size_bytes FROM journal_entries WHERE stream='client'").fetchone()[0]
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", size * 2 - 1)
    assert history.append_entry(replace(first, request_id=2))
    assert server.recent_events() == (event,)
