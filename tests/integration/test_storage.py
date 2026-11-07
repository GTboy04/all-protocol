from dataclasses import replace
from datetime import datetime
import json

from all_protocol import storage
from all_protocol.application.contracts import ErrorKind, JournalEntry
from all_protocol.storage import ConnectionProfile, LocalHistory


def test_old_journal_without_operation_id_remains_readable(tmp_path):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime(2026, 11, 7), "读取", "point", "read", 1, "成功", operation_id=7)
    history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    record = json.loads(history.journal_path.read_bytes())
    record.pop("operation_id")
    history.journal_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert history.recent_entries() == (replace(entry, operation_id=None),)


def test_device_session_metadata_round_trips_and_r3_journal_stays_readable(tmp_path):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime(2026, 11, 7), "读取", "point", "read", 1, "成功",
                         device_id="device-a", session_id="connection-uuid", generation=3)
    history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    record = json.loads(history.journal_path.read_bytes())
    for name in ("device_id", "session_id", "generation"):
        record.pop(name)
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
    limit = history.journal_path.stat().st_size * 2
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", limit)
    second, third = replace(entry, request_id=2), replace(entry, request_id=3)
    assert not history.append_entry(second)
    assert history.journal_path.stat().st_size == limit
    assert LocalHistory(tmp_path).recent_entries() == (second, entry)
    assert history.append_entry(third)
    assert LocalHistory(tmp_path).recent_entries() == (third,)
    assert LocalHistory(tmp_path).profiles() == (profile,)


def test_journal_recovers_after_partial_write_and_cleans_oversize_on_start(tmp_path, monkeypatch):
    history = LocalHistory(tmp_path)
    entry = JournalEntry(1, datetime.now(), "读取", "站 1", "0x03", 1, "成功")
    history.journal_path.write_bytes(b'{"request_id":')
    assert not history.append_entry(entry)
    assert history.recent_entries() == (entry,)
    monkeypatch.setattr(storage, "LOG_LIMIT_BYTES", history.journal_path.stat().st_size - 1)
    assert history.recent_entries() == ()
    assert not history.journal_path.exists()
