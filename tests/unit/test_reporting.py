from concurrent.futures import CancelledError
import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from zipfile import ZipFile

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.application.contracts import (
    ErrorKind, JournalEntry, VerificationResult, VerificationStatus, WriteResult, WriteStatus,
)
from all_protocol.application.journal import LogRetention, RetainedLog
from all_protocol.application.server_contracts import LifecycleAction, RequestMetadata, ServerEvent, ServerLifecycle
from all_protocol.point_tables import PointDefinition, PointTable
from all_protocol.project import Project, ProjectDevice
from all_protocol.reporting import ReportCancellation, ReportScope, ReportSnapshot, export_report, prepare_report
from all_protocol.storage import ConnectionProfile


ZONE = timezone(timedelta(hours=8), "CST")
FROZEN = datetime(2026, 11, 10, 10, tzinfo=ZONE)
START = FROZEN - timedelta(minutes=10)


def snapshot(*, packets=False):
    mb_profile = ConnectionProfile("modbus-tcp", "192.0.2.1:502", {"timeout_ms": 1000}, 1000)
    s7_profile = ConnectionProfile("s7", "192.0.2.2:102 · Rack 0 / Slot 1", {"timeout_ms": 500}, 1000)
    devices = (
        ProjectDevice("mb", '\t=HYPERLINK("test") <script>中文</script>' + "长字段" * 150, mb_profile),
        ProjectDevice("s7", "工位 & 反馈", s7_profile),
    )
    target = ModbusTarget(1, Area.HR, 4, 1)
    table = PointTable("p", "mb", "传感器表", (PointDefinition("sensor", "温度", target, "℃", .1, 2),))
    project = Project("试运行 <联调>", devices, point_tables=(table,))
    write = JournalEntry(
        9, START, "写入", "DB1", "0x05", 8, "设备已应答", packets=((True, b"\x01"), (False, b"\xFF")),
        protocol="s7", endpoint=s7_profile.endpoint, device_id="s7", session_id="s7-connection",
        write=WriteResult(WriteStatus.ACKNOWLEDGED, (42,)),
    )
    verify = replace(
        write, request_id=10, time=START + timedelta(seconds=1), operation="读取", function="0x04",
        outcome=VerificationStatus.MISMATCH, write=None,
        verification=VerificationResult(VerificationStatus.MISMATCH, "s7-connection", 9, (42,), (40,), (0,)),
    )
    client = JournalEntry(
        3, FROZEN, "读取", "HR", "0x03", 100, "请求超时", ErrorKind.TIMEOUT,
        r'错误 <&> "C:\Users\private account\project\log.txt"', protocol="modbus-tcp",
        endpoint=mb_profile.endpoint, device_id="mb", session_id="mb-connection",
    )
    unscoped = (replace(client, device_id="other", detail="OTHER_PROJECT"),
                replace(client, device_id="", detail="LOCAL_MODE"),
                replace(client, time=START - timedelta(milliseconds=1), detail="BEFORE_WINDOW"))
    client_log = RetainedLog((client, verify, write, *unscoped),
                             LogRetention(rotated_at=START + timedelta(seconds=2), clipped=True))
    servers = (
        ("modbus-tcp", RetainedLog((ServerEvent(
            "modbus-tcp", "127.0.0.1:1502", START + timedelta(seconds=2),
            RequestMetadata("127.0.0.1:30123", "故障注入 · 异常应答 · 异常 0x03", b"\x01", b"\x03",
                            (("function", 3), ("offset", 4)), 1, ErrorKind.PROTOCOL, "listener-mb", 5),
        ),))),
        ("s7", RetainedLog((
            ServerEvent("s7", "127.0.0.1:1102", START + timedelta(seconds=3),
                        RequestMetadata("127.0.0.1:30124", "成功", b"\x03", b"\xFF", (("function", 4),), 3,
                                        session_id="listener-s7", request_id=2)),
            ServerEvent("s7", "127.0.0.1:1102", START + timedelta(milliseconds=500),
                        ServerLifecycle(LifecycleAction.STARTED, "启动监听")),
        ))),
    )
    return ReportSnapshot.capture(project, ReportScope(START, FROZEN, packets=packets), client_log, servers,
                                  START + timedelta(milliseconds=100), FROZEN)


@pytest.mark.parametrize("packets", (False, True))
def test_report_counts_coverage_escape_and_evidence_match_across_formats(tmp_path, packets):
    data = prepare_report(snapshot(packets=packets), ReportCancellation())
    path = tmp_path / "报告.zip"
    export_report(data, path, ReportCancellation())
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"report.html", "records.csv", "records.json"}
        html = archive.read("report.html").decode("utf-8")
        report = json.loads(archive.read("records.json"))
        rows = list(csv.DictReader(StringIO(archive.read("records.csv").decode("utf-8-sig"))))

    assert html.count("data-record=") == len(rows) == len(report["records"]) == report["record_count"] == 6
    assert report["exported_at"].endswith("+08:00") and report["scope"]["start"].endswith("+08:00")
    assert report["statistics"]["client"]["count"] == 3 and report["statistics"]["client"]["failed"] == 1
    assert report["statistics"]["modbus-tcp"]["failure_rate"] == 1
    assert report["statistics"]["s7"]["count"] == 1
    assert report["coverage"][0]["may_be_missing"] and report["coverage"][0]["rotated_at"]
    assert report["coverage"][1]["before_workspace"]
    assert {item["protocol"] for item in report["records"]} == {"modbus-tcp", "s7"}
    assert all(value not in html + data.json.decode() + data.csv.decode("utf-8-sig") for value in
               ("OTHER_PROJECT", "LOCAL_MODE", "BEFORE_WINDOW", "private account"))
    assert "<script>" not in html and "&lt;script&gt;中文&lt;/script&gt;" in html
    assert "错误 &lt;&amp;&gt;" in html and "[本机路径]" in html
    assert "工位 &amp; 反馈" in html and "点位地址" in html
    assert rows[-1]["device_name"].startswith("'\t=HYPERLINK")
    assert report["point_tables"][0]["points"][0]["scale"] == .1
    verification = next(item for item in report["records"] if item["verification"])["verification"]
    assert verification["expected"] == [42] and verification["observed"] == [40]
    assert verification["write_session_id"] == "s7-connection" and verification["original_row"] == 1
    assert 'href="#record-1"' in html
    assert any(row["fault"] == "异常应答" for row in rows)
    assert all(("packets" in row) == packets for row in (*report["records"], *rows))
    assert ("RX  FF" in html) == packets


def test_report_discloses_missing_original_and_does_not_count_empty_window_as_success():
    original = snapshot()
    scope = replace(original.scope, start=START + timedelta(milliseconds=750), end=START + timedelta(seconds=1),
                    servers=False)
    data = prepare_report(replace(original, scope=scope, server_logs=()), ReportCancellation())
    report = json.loads(data.json)
    assert len(report["records"]) == 1
    assert report["records"][0]["verification"]["original_row"] is None
    assert "原写入不在所选记录中" in data.html.decode("utf-8")
    data = prepare_report(replace(original, client_log=RetainedLog(), server_logs=()), ReportCancellation())
    assert json.loads(data.json)["statistics"]["client"]["failure_rate"] is None
    assert "未记录时段无结论" in data.html.decode("utf-8")


def test_export_cancel_mid_write_and_disk_failure_preserve_original(tmp_path, monkeypatch):
    bundle = prepare_report(snapshot(packets=True), ReportCancellation())
    path = tmp_path / "existing.zip"
    path.write_bytes(b"existing")
    cancel = ReportCancellation()
    original_open = ZipFile.open

    def stop_on_second_member(self, name, *args, **kwargs):
        if name == "records.json":
            cancel.cancel()
        return original_open(self, name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ZipFile, "open", stop_on_second_member)
        with pytest.raises(CancelledError):
            export_report(bundle, path, cancel)
    assert path.read_bytes() == b"existing" and list(tmp_path.iterdir()) == [path]

    def fail(*_args):
        raise PermissionError("destination locked")

    monkeypatch.setattr("all_protocol.reporting.os.replace", fail)
    with pytest.raises(PermissionError, match="locked"):
        export_report(bundle, path, ReportCancellation())
    assert path.read_bytes() == b"existing" and list(tmp_path.iterdir()) == [path]
