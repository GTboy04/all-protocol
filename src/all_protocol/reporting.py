"""Freeze project evidence and render a portable, offline report bundle."""

from collections import defaultdict
from concurrent.futures import CancelledError
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from html import escape
from io import StringIO
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from zipfile import ZIP_DEFLATED, ZipFile

from .application.contracts import JournalEntry
from .application.diagnostics import DiagnosticRow, request_stats
from .application.journal import VISIBLE_LOG_ROWS, RetainedLog
from .application.server_contracts import RequestMetadata, ServerEvent


@dataclass(frozen=True, slots=True)
class ReportScope:
    start: datetime
    end: datetime
    clients: bool = True
    servers: bool = True
    packets: bool = False

    def __post_init__(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None or self.start >= self.end:
            raise ValueError("请选择有效的开始和结束时间，开始须早于结束")
        if not self.clients and not self.servers:
            raise ValueError("至少选择一种日志来源")


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    id: str
    name: str
    protocol: str
    endpoint: str
    timeout_ms: int
    interval_ms: int


@dataclass(frozen=True, slots=True)
class PointSummary:
    id: str
    name: str
    target: str
    unit: str
    scale: float
    offset: float


@dataclass(frozen=True, slots=True)
class TableSummary:
    id: str
    name: str
    device_id: str
    interval_ms: int
    stale_after_ms: int
    points: tuple[PointSummary, ...]


@dataclass(frozen=True, slots=True)
class ServerSummary:
    protocol: str
    endpoint: str
    points: int
    scenario_points: int


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    name: str
    frozen_at: datetime
    workspace_opened_at: datetime
    scope: ReportScope
    devices: tuple[DeviceSummary, ...]
    tables: tuple[TableSummary, ...]
    server_plans: tuple[ServerSummary, ...]
    client_log: RetainedLog[JournalEntry]
    server_logs: tuple[tuple[str, RetainedLog[ServerEvent]], ...]

    @classmethod
    def capture(cls, project, scope, client_log, server_logs, workspace_opened_at, frozen_at):
        if frozen_at.utcoffset() is None or scope.end > frozen_at:
            raise ValueError("结束时间不能晚于冻结时间")
        devices = tuple(DeviceSummary(device.id, device.name, device.profile.protocol, device.profile.endpoint,
                                      device.profile.fields["timeout_ms"], device.profile.interval_ms)
                        for device in project.devices) if scope.clients else ()
        tables = tuple(TableSummary(table.id, table.name, table.device_id, table.interval_ms, table.stale_after_ms,
                                    tuple(PointSummary(point.id, point.name, point.target.label, point.unit,
                                                       point.scale, point.offset) for point in table.points))
                       for table in project.point_tables) if scope.clients else ()
        plans = ()
        if scope.servers:
            modbus_points = tuple(point for station in project.modbus.stations for point in station.points)
            plans = tuple(ServerSummary(protocol, _endpoint(plan.host, plan.port), len(points),
                                        sum(point.simulation is not None for point in points))
                          for protocol, plan, points in (("modbus-tcp", project.modbus, modbus_points),
                                                          ("s7", project.s7, project.s7.points)))
        return cls(project.name, frozen_at, workspace_opened_at, scope, devices, tables, plans,
                   client_log if scope.clients else RetainedLog(), tuple(server_logs) if scope.servers else ())


def _endpoint(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class ReportCancellation:
    def __init__(self):
        self._event = Event()
        self._lock = Lock()
        self._publishing = False

    def cancel(self) -> bool:
        with self._lock:
            if self._publishing:
                return False
            self._event.set()
            return True

    def check(self):
        if self._event.is_set():
            raise CancelledError()

    def begin_publish(self):
        with self._lock:
            self.check()
            self._publishing = True


@dataclass(frozen=True, slots=True)
class ReportPage:
    title: str
    html: str
    first_record: int = 0


@dataclass(frozen=True, slots=True)
class ReportBundle:
    html: bytes
    json: bytes
    csv: bytes
    pages: tuple[ReportPage, ...]
    record_count: int


_PATH = re.compile(
    r"""(?P<quote>['"])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))[^'"\r\n]*(?P=quote)"""
    r"""|(?<![\w:/])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n<>"']+"""
    r"""|(?<![\w:/])/(?:[^\s/<>:"']+/)+[^\s<>"']*"""
)
_FORMULA = re.compile(r"^[\s\x00-\x20\u200b-\u200f\ufeff]*[=+\-@＝＋－＠]")


def _redact(text):
    return _PATH.sub("[本机路径]", text)


def _iso(value, zone):
    return value.astimezone(zone).isoformat(timespec="milliseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _values(values):
    return [str(value) if type(value) is float and not isfinite(value) else value for value in values]


def _record(entry, row, stream, zone, *, packets, device=None):
    record = {
        "time": _iso(entry.time, zone), "source": stream, "protocol": entry.protocol,
        "endpoint": entry.endpoint, "device_id": device.id if device else "",
        "device_name": device.name if device else "", "peer": "",
        "session_id": "", "request_id": None, "operation_id": None, "generation": None, "kind": "request",
        "operation": row.operation, "target": row.target, "function": row.function,
        "outcome": _redact(row.outcome), "error": row.error, "elapsed_ms": row.elapsed_ms,
        "detail": "", "fault": "", "write": None, "verification": None, "server_details": None,
    }
    if isinstance(entry, JournalEntry):
        record.update(session_id=entry.session_id, request_id=entry.request_id, operation_id=entry.operation_id,
                      generation=entry.generation, outcome=_redact(entry.outcome),
                      detail=_redact(entry.detail), kind="verification" if entry.verification else
                      "connection" if entry.operation in ("连接", "断开") else "request")
        if entry.write is not None:
            write = entry.write
            record["write"] = {"status": write.status, "raw_values": _values(write.values),
                               "engineering_values": write.engineering_values, "error_code": write.error_code,
                               "options": asdict(write.options) if write.options else None}
        if entry.verification is not None:
            verification = entry.verification
            record["verification"] = {**asdict(verification), "expected": _values(verification.expected),
                                      "observed": _values(verification.observed)}
        if packets:
            record["packets"] = [{"direction": "TX" if sending else "RX", "hex": payload.hex(" ").upper()}
                                 for sending, payload in entry.packets]
    elif isinstance(payload := entry.payload, RequestMetadata):
        fault = "故障注入 · "
        record.update(peer=payload.peer, session_id=payload.session_id, request_id=payload.request_id,
                      fault=payload.result[len(fault):].split(" · ", 1)[0] if payload.result.startswith(fault) else "",
                      server_details=dict(payload.details))
        if packets:
            record["packets"] = [{"direction": label, "hex": data.hex(" ").upper()}
                                 for label, data in (("RX", payload.rx), ("TX", payload.tx)) if data]
    else:
        record.update(kind="lifecycle", server_details={"action": payload.action})
        if packets:
            record["packets"] = []
    return record


def _collect(snapshot, cancel):
    scope, zone = snapshot.scope, snapshot.frozen_at.tzinfo
    devices = {device.id: device for device in snapshot.devices}
    selected, coverage, statistics = [], [], {}
    streams = ([("client", snapshot.client_log)] if scope.clients else []) + list(snapshot.server_logs)
    for stream, log in streams:
        count_before = len(selected)
        rows = []
        times = []
        retained_start = min((item.time.astimezone(zone) for item in log.entries), default=None)
        for entry in log.entries:
            cancel.check()
            stamp = entry.time.astimezone(zone)
            if not scope.start <= stamp <= scope.end:
                continue
            device = devices.get(entry.device_id) if stream == "client" else None
            if stream == "client" and device is None:
                continue
            row = (DiagnosticRow.from_client(replace(entry, packets=()), device.name)
                   if device else DiagnosticRow.from_server(entry))
            selected.append((stamp, _record(entry, row, stream, zone, packets=scope.packets, device=device)))
            rows.append(row)
            times.append(stamp)
        retention = log.retention
        rotated = retention.rotated_at
        before_workspace = stream != "client" and scope.start < snapshot.workspace_opened_at
        clipped = retention.clipped and (retained_start is None or scope.start <= retained_start)
        may_be_missing = (rotated is not None and scope.start <= rotated.astimezone(zone)
                          or clipped or before_workspace or retention.incomplete)
        coverage.append({"source": stream, "record_count": len(selected) - count_before,
                         "first": _iso(min(times), zone) if times else None,
                         "last": _iso(max(times), zone) if times else None,
                         "retained_start": _iso(retained_start, zone) if retained_start else None,
                         "rotated_at": _iso(rotated, zone) if rotated else None,
                         "buffer_clipped": retention.clipped, "storage_incomplete": retention.incomplete,
                         "before_workspace": before_workspace, "may_be_missing": may_be_missing})
        stats = request_stats(rows)
        statistics[stream] = {**asdict(stats), "failure_rate": stats.failed / stats.count if stats.count else None}
    records = [record for _, record in sorted(selected, key=lambda item: item[0])]
    writes = {(record["device_id"], record["session_id"], record["request_id"]): index
              for index, record in enumerate(records, 1) if record["write"] is not None}
    for index, record in enumerate(records, 1):
        record["row"] = index
        if (verification := record["verification"]) is not None:
            key = (record["device_id"], verification["write_session_id"], verification["write_request_id"])
            verification["original_row"] = writes.get(key)
    return records, coverage, statistics


_COLUMNS = (
    "row", "time", "source", "kind", "protocol", "device_id", "device_name", "endpoint", "peer", "session_id",
    "request_id", "operation_id", "generation", "operation", "target", "function", "outcome", "error", "elapsed_ms",
    "detail", "fault", "write", "verification", "server_details",
)


def _csv_cell(value):
    if isinstance(value, (dict, list, tuple)):
        value = _json(value)
    if isinstance(value, str) and (value[:1] in ("\t", "\r", "\n") or _FORMULA.match(value)):
        return "'" + value
    return value


def _text(value):
    return escape("—" if value is None or value == "" else str(value))


def _table(headers, rows):
    head = "".join(f"<th>{_text(label)}</th>" for label in headers)
    return (f'<table border="1" cellspacing="0" cellpadding="5"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>")


def _row(*cells):
    return "<tr>" + "".join(f"<td>{_text(cell)}</td>" for cell in cells) + "</tr>"


def _source_name(source):
    return {"client": "客户端", "modbus-tcp": "Modbus TCP 服务端", "s7": "S7 服务端"}.get(source, source)


def _record_row(record):
    index = record["row"]
    address = "\n".join(filter(None, (record["device_name"], record["endpoint"], record["peer"], record["target"])))
    context = "\n".join(filter(None, (_source_name(record["source"]), record["protocol"], record["operation"],
                                      record["function"], record["session_id"])))
    if record["request_id"] is not None:
        context += f"\n请求 #{record['request_id']}"
    result = "\n".join(dict.fromkeys(filter(None, (record["outcome"], record["error"], record["detail"]))))
    if record["elapsed_ms"] is not None:
        result += f"\n{record['elapsed_ms']} ms"
    evidence = ""
    if record["fault"]:
        evidence += f"<p>故障注入 · {_text(record['fault'])}</p>"
    if (write := record["write"]) is not None:
        evidence += f"<p>{_text(write['status'])}<br>原始值 {_text(_json(write['raw_values']))}"
        if write["engineering_values"]:
            evidence += f"<br>工程值 {_text(_json(write['engineering_values']))}"
        if write["options"] and any(type(value) is float for value in write["raw_values"]):
            options = write["options"]
            evidence += (f"<br>绝对容差 {_text(options['absolute_tolerance'])}"
                         f" · 相对容差 {_text(options['relative_tolerance'])}")
        evidence += "</p>"
    if (check := record["verification"]) is not None:
        original = check["original_row"]
        link = f'<a href="#record-{original}">原写入 · 第 {original} 条</a>' if original else "原写入不在所选记录中"
        method = "独立读取" if check["independent"] else "自动回读"
        evidence += (f"<p>{_text(check['status'])} · {method}<br>预期 {_text(_json(check['expected']))}"
                     f"<br>实测 {_text(_json(check['observed']))}<br>{link}"
                     f"<br>{_text(check['write_session_id'])} / 请求 #{check['write_request_id']}</p>")
    if record.get("packets"):
        packets = "\n".join(f"{item['direction']}  {item['hex']}" for item in record["packets"])
        evidence += f"<pre>{_text(packets)}</pre>"
    return (f'<tr id="record-{index}" data-record="{index}"><td>{index}<br>{_text(record["time"])}</td>'
            f"<td>{_text(context)}</td><td>{_text(address)}</td><td>{_text(result)}</td><td>{evidence or '—'}</td></tr>")


_STYLE = """
body{font:15px/1.5 system-ui,sans-serif;color:#182b38;background:#f5f7f8;max-width:1420px;margin:2rem auto;padding:0 1rem}
h1,h2{color:#143e49} h1{border-bottom:4px solid #16796f;padding-bottom:.4rem}
table{border-collapse:collapse;table-layout:fixed;width:100%;background:#fff;margin:1rem 0 2rem}
th,td{border:1px solid #cbd7dc;padding:.5rem;vertical-align:top;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}
th{background:#dcecea} tr:nth-child(even){background:#f2f7f7}
pre{font-size:.85em;white-space:pre-wrap;overflow-wrap:anywhere} a{color:#12685f}
@media print{body{background:white;margin:0}thead{display:table-header-group}tr{break-inside:avoid}}
"""


def _pages(snapshot, records, coverage, stats, cancel):
    zone, scope = snapshot.frozen_at.tzinfo, snapshot.scope
    zone_name = snapshot.frozen_at.strftime("%Z (%z)")
    overview = (f"<h1>{_text(snapshot.name)} · 联调报告</h1>"
                f"<p>导出快照冻结于 {_iso(snapshot.frozen_at, zone)} · {_text(zone_name)}</p>"
                f"<p>所选时间范围 {_iso(scope.start, zone)} 至 {_iso(scope.end, zone)}（含边界）</p>"
                f"<p><b>{len(records)} 条记录</b> · {'含' if scope.packets else '不含'}原始报文。"
                f"每个来源最多 {VISIBLE_LOG_ROWS} 条。客户端按项目设备 ID 匹配，"
                "服务端仅含本次工作区记录。较早的日志可能缺少轮换标记。未记录时段无结论。</p>"
                "<p>统计按来源分别计算，客户端和服务端可能记录同一次通信。"
                "仅计读写，排除输入错误和未发送的取消项，缺失耗时的旧记录仅计数量。</p>")
    coverage_rows = []
    for item in coverage:
        status = []
        if item["rotated_at"]:
            status.append(f"轮换 {_text(item['rotated_at'])}")
        if item["buffer_clipped"]:
            status.append("超过保留条数")
        if item["before_workspace"]:
            status.append("包含本次工作区打开前的时段")
        if item["storage_incomplete"]:
            status.append("日志读取或保存异常")
        if item["may_be_missing"]:
            status.append("所选范围可能缺失")
        coverage_rows.append(_row(_source_name(item["source"]), item["record_count"], item["first"], item["last"],
                                  " · ".join(status) or "无已知轮换或截断"))
    overview += _table(("来源", "记录数", "首条", "末条", "范围与缺失"), coverage_rows)
    stats_rows = []
    for source, item in stats.items():
        average = f"{item['average_ms']:.1f}" if item["average_ms"] is not None else "—"
        rate = f"{item['failure_rate']:.1%}" if item["failure_rate"] is not None else "—"
        stats_rows.append(_row(_source_name(source), item["count"], item["failed"], rate, item["unknown_writes"],
                               item["durations"], average, item["p50_ms"], item["p95_ms"]))
    overview += _table(("来源", "读写", "失败", "失败率", "未知写入", "有耗时", "平均 ms", "P50 ms", "P95 ms"), stats_rows)
    pages = [ReportPage("概览与统计", overview)]

    def add_pages(title, headers, rows, *, records=False):
        size = 20
        for offset in range(0, len(rows), size):
            cancel.check()
            label = f"{title} {offset + 1}–{min(offset + size, len(rows))}"
            pages.append(ReportPage(label, f"<h2>{label}</h2>" + _table(headers, rows[offset:offset + size]),
                                    offset + 1 if records else 0))

    add_pages("设备配置", ("设备", "ID", "协议", "端点", "超时 ms", "轮询 ms"),
              [_row(device.name, device.id, device.protocol, device.endpoint, device.timeout_ms, device.interval_ms)
               for device in snapshot.devices])
    add_pages("点表", ("点表", "设备 ID", "点数", "轮询 ms", "过期 ms"),
              [_row(table.name, table.device_id, len(table.points), table.interval_ms, table.stale_after_ms)
               for table in snapshot.tables])
    add_pages("点位地址", ("点表", "点位", "目标", "单位", "倍率", "偏置"),
              [_row(table.name, point.name, point.target, point.unit, point.scale, point.offset)
               for table in snapshot.tables for point in table.points])
    add_pages("项目已应用的监听配置", ("协议", "端点", "点数", "场景点数"),
              [_row(plan.protocol, plan.endpoint, plan.points, plan.scenario_points) for plan in snapshot.server_plans])
    rows = []
    for record in records:
        cancel.check()
        rows.append(_record_row(record))
    add_pages("联调记录", ("序号与时间", "请求", "设备与地址", "结果", "验证与报文"), rows, records=True)
    return tuple(pages)


def prepare_report(snapshot: ReportSnapshot, cancel: ReportCancellation) -> ReportBundle:
    cancel.check()
    records, coverage, stats = _collect(snapshot, cancel)
    pages = _pages(snapshot, records, coverage, stats, cancel)
    zone = snapshot.frozen_at.tzinfo
    payload = {
        "format": "all-protocol-report", "schema_version": 1, "project": snapshot.name,
        "exported_at": _iso(snapshot.frozen_at, zone), "time_zone": snapshot.frozen_at.strftime("%Z (%z)"),
        "scope": {"start": _iso(snapshot.scope.start, zone), "end": _iso(snapshot.scope.end, zone),
                  "clients": snapshot.scope.clients, "servers": snapshot.scope.servers, "packets": snapshot.scope.packets},
        "workspace_opened_at": _iso(snapshot.workspace_opened_at, zone), "retained_limit_per_source": VISIBLE_LOG_ROWS,
        "devices": [asdict(device) for device in snapshot.devices], "point_tables": [asdict(table) for table in snapshot.tables],
        "server_plans": [asdict(plan) for plan in snapshot.server_plans], "coverage": coverage, "statistics": stats,
        "record_count": len(records), "records": records,
    }
    json_bytes = _json(payload).encode("utf-8")
    output = StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    columns = _COLUMNS + (("packets",) if snapshot.scope.packets else ())
    writer.writerow(columns)
    for record in records:
        cancel.check()
        writer.writerow(_csv_cell(record[column]) for column in columns)
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width"><title>联调报告</title><style>' + _STYLE + "</style><body>"
            + "".join(page.html for page in pages) + "</body></html>")
    cancel.check()
    return ReportBundle(html.encode("utf-8"), json_bytes, output.getvalue().encode("utf-8-sig"), pages, len(records))


def export_report(bundle: ReportBundle, path: Path, cancel: ReportCancellation):
    cancel.check()
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".allprotocol-report-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                for name, data in (("report.html", bundle.html), ("records.json", bundle.json), ("records.csv", bundle.csv)):
                    with archive.open(name, "w") as member:
                        for offset in range(0, len(data), 65536):
                            cancel.check()
                            member.write(data[offset:offset + 65536])
            output.flush()
            os.fsync(output.fileno())
        cancel.begin_publish()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
