import json
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from os import SEEK_END
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

from .adapters.modbus_tcp.model import Area
from .adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from .adapters.modbus_tcp.values import DataType, RegisterOrder
from .adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Target
from .adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from .application.contracts import ErrorKind, JournalEntry
from .application.server_contracts import LifecycleAction, RequestMetadata, ServerEvent, ServerLifecycle
from .application.simulation import decode_rule


LOG_LIMIT_BYTES = 5 * 1024 * 1024
VISIBLE_LOG_ROWS = 500
type ProfileFields = dict[str, str | int]


@dataclass(frozen=True)
class ConnectionProfile:
    protocol: str
    endpoint: str
    fields: ProfileFields
    interval_ms: int

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.endpoint


def _json_bytes(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _replace_file(path: Path, contents: bytes):
    file = QSaveFile(str(path))
    if not file.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(file.errorString())
    if file.write(contents) != len(contents):
        file.cancelWriting()
        raise OSError(file.errorString())
    if not file.commit():
        raise OSError(file.errorString())


def _load_profile(data) -> ConnectionProfile | None:
    if not isinstance(data, dict):
        return None
    protocol, endpoint = data.get("protocol"), data.get("endpoint")
    fields, interval = data.get("fields"), data.get("interval_ms", 1000)
    if (isinstance(protocol, str) and protocol and isinstance(endpoint, str) and endpoint
            and isinstance(fields, dict) and all(type(value) in (str, int) for value in fields.values())
            and type(interval) is int and 200 <= interval <= 30000):
        return ConnectionProfile(protocol, endpoint, fields, interval)
    return None


def _load_entry(line: bytes) -> JournalEntry:
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("Expected a journal record")
    for field in ("operation", "target", "function", "outcome", "detail", "protocol", "endpoint"):
        if not isinstance(data[field], str):
            raise ValueError(f"Invalid journal field {field}")
    if type(data["request_id"]) is not int or type(data["elapsed_ms"]) is not int:
        raise ValueError("Invalid journal number")
    if data.get("operation_id") is not None and type(data["operation_id"]) is not int:
        raise ValueError("Invalid operation ID")
    for field in ("device_id", "session_id"):
        if field in data and not isinstance(data[field], str):
            raise ValueError(f"Invalid journal field {field}")
    if "generation" in data and type(data["generation"]) is not int:
        raise ValueError("Invalid session generation")
    data["time"] = datetime.fromisoformat(data["time"])
    data["error"] = ErrorKind(data["error"]) if data["error"] is not None else None
    data["packets"] = tuple((sending, bytes.fromhex(payload)) for sending, payload in data["packets"])
    return JournalEntry(**data)


@contextmanager
def _locked(directory: Path, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / name))
    if not lock.tryLock(250):
        raise OSError(f"无法锁定本地数据目录（{lock.error().name}）")
    try:
        yield
    finally:
        lock.unlock()


def _read_journal(path: Path, decode):
    if not path.exists():
        return ()
    if path.stat().st_size > LOG_LIMIT_BYTES:
        path.unlink()
        return ()
    recent = deque(maxlen=VISIBLE_LOG_ROWS)
    with path.open("rb") as journal:
        for line in journal:
            try:
                recent.append(decode(line))
            except (ValueError, TypeError, KeyError):
                continue
    return tuple(reversed(recent))


def _append_journal(path: Path, record: dict) -> bool:
    line = _json_bytes(record)
    if len(line) > LOG_LIMIT_BYTES:
        raise OSError("单条日志超过 5 MB")
    with path.open("a+b") as journal:
        size = journal.seek(0, SEEK_END)
        separator = b""
        if size:
            journal.seek(-1, SEEK_END)
            if journal.read(1) != b"\n":
                separator = b"\n"
        if size + len(separator) + len(line) <= LOG_LIMIT_BYTES:
            journal.write(separator + line)
            return False
    _replace_file(path, line)
    return True


class LocalHistory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.profiles_path = directory / "connections.json"
        self.journal_path = directory / "requests.jsonl"

    def _locked(self):
        return _locked(self.directory, "history.lock")

    def _read_profiles(self) -> tuple[ConnectionProfile, ...]:
        try:
            data = json.loads(self.profiles_path.read_bytes())
        except (FileNotFoundError, ValueError):
            return ()
        if not isinstance(data, list):
            return ()
        return tuple(profile for item in data if (profile := _load_profile(item)) is not None)

    def profiles(self) -> tuple[ConnectionProfile, ...]:
        with self._locked():
            return self._read_profiles()

    def _write_profiles(self, profiles: tuple[ConnectionProfile, ...]):
        contents = json.dumps([asdict(item) for item in profiles], ensure_ascii=False, indent=2)
        _replace_file(self.profiles_path, (contents + "\n").encode("utf-8"))

    def remember(self, profile: ConnectionProfile) -> tuple[ConnectionProfile, ...]:
        with self._locked():
            profiles = self._read_profiles()
            if profiles and profiles[0] == profile:
                return profiles
            updated = (profile,) + tuple(item for item in profiles if item.key != profile.key)
            self._write_profiles(updated)
            return updated

    def forget(self, profile: ConnectionProfile) -> tuple[ConnectionProfile, ...]:
        with self._locked():
            profiles = self._read_profiles()
            updated = tuple(item for item in profiles if item.key != profile.key)
            if updated != profiles:
                self._write_profiles(updated)
            return updated

    def recent_entries(self) -> tuple[JournalEntry, ...]:
        with self._locked():
            return _read_journal(self.journal_path, _load_entry)

    def append_entry(self, entry: JournalEntry) -> bool:
        """Append an entry and report whether the previous journal was cleared."""
        record = asdict(entry)
        record["time"] = entry.time.isoformat()
        record["packets"] = [(sending, packet.hex()) for sending, packet in entry.packets]
        with self._locked():
            return _append_journal(self.journal_path, record)


def _text(data: dict, field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise ValueError(f"Invalid server journal field {field}")
    return value


def _request_metadata(data: dict, details: dict) -> RequestMetadata:
    elapsed = data.get("elapsed_ms")
    if elapsed is not None and (type(elapsed) is not int or elapsed < 0):
        raise ValueError("Invalid server request duration")
    error = data.get("error")
    session_id = _text(data, "session_id") if "session_id" in data else ""
    request_id = data.get("request_id")
    if request_id is not None and (type(request_id) is not int or request_id < 1):
        raise ValueError("Invalid server request ID")
    return RequestMetadata(
        _text(data, "peer"), _text(data, "result"),
        bytes.fromhex(_text(data, "rx")), bytes.fromhex(_text(data, "tx")), tuple(details.items()),
        elapsed, ErrorKind(error) if error is not None else None, session_id, request_id,
    )


class _ServerJournal:
    _journal_protocol: str
    _journal_lock: str
    _legacy_detail_types: dict
    directory: Path
    journal_path: Path

    def _decode_event(self, line: bytes) -> ServerEvent:
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("Expected a server journal record")
        time = datetime.fromisoformat(_text(data, "time"))
        if "schema_version" not in data:
            details = {field: data[field] for field in self._legacy_detail_types}
            if any(type(value) not in self._legacy_detail_types[field] for field, value in details.items()):
                raise ValueError("Invalid legacy server request details")
            return ServerEvent(self._journal_protocol, "", time, _request_metadata(data, details))

        if (type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["protocol"] != self._journal_protocol):
            raise ValueError("Unknown server journal version or protocol")
        endpoint = _text(data, "endpoint")
        if data["kind"] == "lifecycle":
            payload = ServerLifecycle(LifecycleAction(data["action"]), _text(data, "result"))
        elif data["kind"] == "request":
            details = data["details"]
            if not isinstance(details, dict) or any(type(value) not in (str, int, bool, float, type(None))
                                                    for value in details.values()):
                raise ValueError("Invalid server request details")
            payload = _request_metadata(data, details)
        else:
            raise ValueError("Unknown server event kind")
        return ServerEvent(self._journal_protocol, endpoint, time, payload)

    def recent_events(self) -> tuple[ServerEvent, ...]:
        with _locked(self.directory, self._journal_lock):
            return _read_journal(self.journal_path, self._decode_event)

    def append_event(self, event: ServerEvent) -> bool:
        if event.protocol != self._journal_protocol:
            raise ValueError("Server event belongs to a different protocol")
        record = {"schema_version": 1, "protocol": event.protocol, "endpoint": event.endpoint,
                  "time": event.time.isoformat()}
        payload = event.payload
        if isinstance(payload, RequestMetadata):
            record.update(kind="request", peer=payload.peer, result=payload.result, rx=payload.rx.hex(),
                          tx=payload.tx.hex(), details=dict(payload.details), elapsed_ms=payload.elapsed_ms,
                          error=payload.error, session_id=payload.session_id, request_id=payload.request_id)
        elif isinstance(payload, ServerLifecycle):
            record.update(kind="lifecycle", action=payload.action, result=payload.result)
        else:
            raise ValueError("Only request and lifecycle events can be stored")
        with _locked(self.directory, self._journal_lock):
            return _append_journal(self.journal_path, record)


def _point_record(point) -> dict:
    record = asdict(point)
    if point.simulation is None:
        del record["simulation"]
    else:
        record["simulation"]["actions"] = list(record["simulation"]["actions"])
    return record


def encode_modbus_plan(plan: ServerPlan) -> dict:
    stations = [{"unit_id": station.unit_id, "name": station.name,
                 "points": [_point_record(point) for point in station.points]} for station in plan.stations]
    has_rules = any("simulation" in point for station in stations for point in station["points"])
    return {"schema_version": 3 if has_rules else 2, "protocol": "modbus-tcp",
            "host": plan.host, "port": plan.port, "stations": stations}


def decode_modbus_plan(data) -> ServerPlan:
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] not in (1, 2, 3) or data.get("protocol") != "modbus-tcp"):
        raise ValueError("方案版本或协议无法识别")
    if not isinstance(data["stations"], list):
        raise ValueError("从站必须是列表")
    for station in data["stations"]:
        if not isinstance(station, dict) or not isinstance(station.get("points"), list):
            raise ValueError("从站点位必须是列表")
    stations = tuple(
        Station(station["unit_id"], station["name"], tuple(
            ServerPoint(
                Area(point["area"]), point["offset"], point["initial"], point["name"],
                DataType(point["data_type"]) if point.get("data_type") is not None else None,
                RegisterOrder(point.get("order", RegisterOrder.BIG)),
                decode_rule(point.get("simulation")),
            )
            for point in station["points"]
        ))
        for station in data["stations"]
    )
    plan = ServerPlan(data["host"], data["port"], stations)
    plan.validate()
    return plan


class ServerStorage(_ServerJournal):
    _journal_protocol = "modbus-tcp"
    _journal_lock = "server-journal.lock"
    _legacy_detail_types = dict.fromkeys(("unit_id", "function", "offset", "count"), (int, type(None)))

    def __init__(self, directory: Path):
        self.directory = directory
        self.plan_path = directory / "modbus-tcp-server.json"
        self.journal_path = directory / "server-requests.jsonl"

    def load_plan(self) -> ServerPlan:
        try:
            data = json.loads(self.plan_path.read_bytes())
        except FileNotFoundError:
            return ServerPlan()
        return decode_modbus_plan(data)

    def save_plan(self, plan: ServerPlan):
        plan.validate()
        record = encode_modbus_plan(plan)
        with _locked(self.directory, "server-plan.lock"):
            _replace_file(self.plan_path, _json_bytes(record))

def encode_s7_plan(plan: S7ServerPlan) -> dict:
    points = [_point_record(point) for point in plan.points]
    has_rules = any("simulation" in point for point in points)
    return {"schema_version": 2 if has_rules else 1, "protocol": "s7",
            "host": plan.host, "port": plan.port, "points": points}


def decode_s7_plan(data) -> S7ServerPlan:
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] not in (1, 2) or data.get("protocol") != "s7"):
        raise ValueError("S7 方案版本或协议无法识别")
    if not isinstance(data["points"], list):
        raise ValueError("点位必须是列表")
    points = []
    for point in data["points"]:
        target = point["target"]
        points.append(S7ServerPoint(
            S7Target(S7Area(target["area"]), target["db"], target["offset"],
                     S7DataType(target["data_type"]), target["count"], target["bit"]),
            point["initial"], point["name"], decode_rule(point.get("simulation")),
        ))
    plan = S7ServerPlan(data["host"], data["port"], tuple(points))
    plan.validate()
    return plan


class S7ServerStorage(_ServerJournal):
    _journal_protocol = "s7"
    _journal_lock = "s7-server-journal.lock"
    _legacy_detail_types = {"function": (int, type(None)), "target": (str,)}

    def __init__(self, directory: Path):
        self.directory = directory
        self.plan_path = directory / "s7-server.json"
        self.journal_path = directory / "s7-server-requests.jsonl"

    def load_plan(self) -> S7ServerPlan:
        try:
            data = json.loads(self.plan_path.read_bytes())
        except FileNotFoundError:
            return S7ServerPlan()
        return decode_s7_plan(data)

    def save_plan(self, plan: S7ServerPlan):
        plan.validate()
        record = encode_s7_plan(plan)
        with _locked(self.directory, "s7-server-plan.lock"):
            _replace_file(self.plan_path, _json_bytes(record))
