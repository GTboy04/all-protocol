"""Local SQLite repositories and codecs shared with portable project files.

The *_path attributes naming JSON/JSONL files are migration sources only.
All current writes go to database_path.
"""

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

from .adapters.modbus_tcp.model import Area, ModbusTarget
from .adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from .adapters.modbus_tcp.values import DataType, RegisterOrder
from .adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Target
from .adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from .application.contracts import (
    ErrorKind, JournalEntry, VerificationOptions, VerificationResult, VerificationStatus, WriteResult, WriteStatus,
)
from .application.journal import VISIBLE_LOG_ROWS, LogRetention, RetainedLog
from .application.server_contracts import LifecycleAction, RequestMetadata, ServerEvent, ServerLifecycle
from .application.simulation import decode_rule
from .point_tables import decode_target, target_record
from .database import DATABASE_NAME, connect, dumps, get_meta, set_meta, read_plan, write_plan


LOG_LIMIT_BYTES = 5 * 1024 * 1024
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


def _load_entry(data: dict) -> JournalEntry:
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
    if (write := data.get("write")) is not None:
        if not isinstance(write, dict):
            raise ValueError("Invalid write evidence")
        target = write["target"]
        options = write["options"]
        error_code = write["error_code"]
        if error_code is not None and type(error_code) is not int:
            raise ValueError("Invalid write error code")
        data["write"] = WriteResult(
            WriteStatus(write["status"]), _value_tuple(write["values"]),
            decode_target(data["protocol"], target, single=False) if target is not None else None,
            VerificationOptions(**options) if options is not None else None,
            _value_tuple(write["engineering_values"], (str,)), error_code,
        )
    if (verification := data.get("verification")) is not None:
        if not isinstance(verification, dict):
            raise ValueError("Invalid verification evidence")
        expected, observed = (_value_tuple(verification[name]) for name in ("expected", "observed"))
        request_id = verification["write_request_id"]
        independent = verification["independent"]
        mismatches = _value_tuple(verification["mismatches"], (int,))
        if (type(request_id) is not int or request_id < 1 or type(independent) is not bool
                or any(not 0 <= index < min(len(expected), len(observed)) for index in mismatches)):
            raise ValueError("Invalid verification reference")
        data["verification"] = VerificationResult(
            VerificationStatus(verification["status"]), _text(verification, "write_session_id"), request_id,
            expected, observed, mismatches, independent,
        )
    return JournalEntry(**data)


def _value_tuple(values, allowed=(int, bool, float, str)):
    if not isinstance(values, list) or any(type(value) not in allowed for value in values):
        raise ValueError("Invalid journal values")
    return tuple(values)


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


def _insert_journal(connection, stream, record):
    payload = dumps(record)
    size = len((payload + "\n").encode("utf-8"))
    connection.execute(
        "INSERT INTO journal_entries(stream, time, protocol, endpoint, payload, size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
        (stream, record["time"], record.get("protocol", ""), record.get("endpoint", ""), payload, size),
    )


def _migrate_journal(connection, stream, path, decode):
    marker = "migrated:" + path.name
    if get_meta(connection, marker) is not None:
        return
    connection.execute("INSERT OR IGNORE INTO journal_state(stream) VALUES (?)", (stream,))
    if path.exists():
        if path.stat().st_size > LOG_LIMIT_BYTES:
            connection.execute("UPDATE journal_state SET rotated_at=? WHERE stream=?",
                               (datetime.now().astimezone().isoformat(), stream))
        else:
            with path.open("rb") as journal:
                for line in journal:
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("Expected a journal record")
                        if set(record) == {"journal_rotated_at"}:
                            rotated = datetime.fromisoformat(record["journal_rotated_at"]).isoformat()
                            connection.execute("UPDATE journal_state SET rotated_at=? WHERE stream=?", (rotated, stream))
                        else:
                            decode(dict(record))
                            _insert_journal(connection, stream, record)
                    except (ValueError, TypeError, KeyError):
                        connection.execute("UPDATE journal_state SET incomplete=1 WHERE stream=?", (stream,))
    set_meta(connection, marker, "1")


def _read_database_log(directory, stream, legacy_path, decode):
    with connect(directory / DATABASE_NAME) as connection:
        _migrate_journal(connection, stream, legacy_path, decode)
        size, count = connection.execute(
            "SELECT coalesce(sum(size_bytes), 0), count(*) FROM journal_entries WHERE stream=?", (stream,)
        ).fetchone()
        if size > LOG_LIMIT_BYTES:
            connection.execute("DELETE FROM journal_entries WHERE stream=?", (stream,))
            connection.execute("UPDATE journal_state SET rotated_at=?, incomplete=0 WHERE stream=?",
                               (datetime.now().astimezone().isoformat(), stream))
            count = 0
        rotated, incomplete = connection.execute(
            "SELECT rotated_at, incomplete FROM journal_state WHERE stream=?", (stream,)
        ).fetchone()
        recent = []
        for (payload,) in connection.execute(
            "SELECT payload FROM journal_entries WHERE stream=? ORDER BY id DESC LIMIT ?", (stream, VISIBLE_LOG_ROWS)
        ):
            try:
                recent.append(decode(json.loads(payload)))
            except (ValueError, TypeError, KeyError):
                incomplete = True
        return RetainedLog(tuple(recent), LogRetention(
            datetime.fromisoformat(rotated) if rotated else None, count > VISIBLE_LOG_ROWS, bool(incomplete)))


def _append_database_log(directory, stream, legacy_path, decode, record):
    size = len(_json_bytes(record))
    if size > LOG_LIMIT_BYTES:
        raise OSError("单条日志超过 5 MB")
    with connect(directory / DATABASE_NAME) as connection:
        _migrate_journal(connection, stream, legacy_path, decode)
        total = connection.execute(
            "SELECT coalesce(sum(size_bytes), 0) FROM journal_entries WHERE stream=?", (stream,)
        ).fetchone()[0]
        rotated = total + size > LOG_LIMIT_BYTES
        if rotated:
            connection.execute("DELETE FROM journal_entries WHERE stream=?", (stream,))
            connection.execute("UPDATE journal_state SET rotated_at=?, incomplete=0 WHERE stream=?",
                               (datetime.now().astimezone().isoformat(), stream))
        _insert_journal(connection, stream, record)
        return rotated


class LocalHistory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.profiles_path = directory / "connections.json"
        self.journal_path = directory / "requests.jsonl"

    @property
    def database_path(self):
        return self.directory / DATABASE_NAME

    def _migrate_profiles(self, connection):
        if get_meta(connection, "migrated:connections.json") is not None:
            return
        try:
            raw = json.loads(self.profiles_path.read_bytes())
        except FileNotFoundError:
            raw = []
        if not isinstance(raw, list):
            raise ValueError("本地连接配置不是列表")
        profiles = tuple(_load_profile(item) for item in raw)
        if any(profile is None for profile in profiles):
            raise ValueError("本地连接配置包含无效记录")
        for profile in reversed(profiles):
            self._store_profile(connection, profile)
        set_meta(connection, "migrated:connections.json", "1")

    @staticmethod
    def _store_profile(connection, profile):
        connection.execute("DELETE FROM connections WHERE protocol=? AND endpoint=?", profile.key)
        connection.execute("INSERT INTO connections(protocol, endpoint, fields, interval_ms) VALUES (?, ?, ?, ?)",
                           (*profile.key, dumps(profile.fields), profile.interval_ms))

    @staticmethod
    def _read_profiles(connection):
        return tuple(ConnectionProfile(protocol, endpoint, json.loads(fields), interval)
                     for protocol, endpoint, fields, interval in connection.execute(
                         "SELECT protocol, endpoint, fields, interval_ms FROM connections ORDER BY position DESC"))

    def profiles(self) -> tuple[ConnectionProfile, ...]:
        with connect(self.database_path) as connection:
            self._migrate_profiles(connection)
            return self._read_profiles(connection)

    def remember(self, profile: ConnectionProfile) -> tuple[ConnectionProfile, ...]:
        if _load_profile(asdict(profile)) is None:
            raise ValueError("连接配置无效")
        with connect(self.database_path) as connection:
            self._migrate_profiles(connection)
            profiles = self._read_profiles(connection)
            if profiles and profiles[0] == profile:
                return profiles
            self._store_profile(connection, profile)
            return self._read_profiles(connection)

    def forget(self, profile: ConnectionProfile) -> tuple[ConnectionProfile, ...]:
        with connect(self.database_path) as connection:
            self._migrate_profiles(connection)
            connection.execute("DELETE FROM connections WHERE protocol=? AND endpoint=?", profile.key)
            return self._read_profiles(connection)

    def recent_entries(self) -> tuple[JournalEntry, ...]:
        return self.read_log().entries

    def read_log(self) -> RetainedLog[JournalEntry]:
        return _read_database_log(self.directory, "client", self.journal_path, _load_entry)

    def append_entry(self, entry: JournalEntry) -> bool:
        """Append an entry and report whether the previous journal was cleared."""
        record = asdict(entry)
        record["time"] = entry.time.isoformat()
        record["packets"] = [(sending, packet.hex()) for sending, packet in entry.packets]
        if entry.write is None:
            del record["write"]
        else:
            target = entry.write.target
            encoded = target_record(target) if isinstance(target, (ModbusTarget, S7Target)) else None
            if isinstance(target, ModbusTarget) and target.data_type is None:
                encoded["data_type"] = None
            record["write"]["target"] = encoded
        if entry.verification is None:
            del record["verification"]
        return _append_database_log(self.directory, "client", self.journal_path, _load_entry, record)


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
    _legacy_detail_types: dict
    directory: Path
    journal_path: Path

    def _decode_event(self, data: dict) -> ServerEvent:
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
        return self.read_log().entries

    def read_log(self) -> RetainedLog[ServerEvent]:
        return _read_database_log(self.directory, self._journal_protocol, self.journal_path, self._decode_event)

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
        return _append_database_log(self.directory, self._journal_protocol, self.journal_path, self._decode_event, record)


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
    _legacy_detail_types = dict.fromkeys(("unit_id", "function", "offset", "count"), (int, type(None)))

    def __init__(self, directory: Path):
        self.directory = directory
        self.plan_path = directory / "modbus-tcp-server.json"
        self.journal_path = directory / "server-requests.jsonl"

    @property
    def database_path(self):
        return self.directory / DATABASE_NAME

    def load_plan(self) -> ServerPlan:
        return _load_database_plan(self, decode_modbus_plan, encode_modbus_plan, ServerPlan)

    def save_plan(self, plan: ServerPlan):
        _save_database_plan(self, plan, encode_modbus_plan)


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
    _legacy_detail_types = {"function": (int, type(None)), "target": (str,)}

    def __init__(self, directory: Path):
        self.directory = directory
        self.plan_path = directory / "s7-server.json"
        self.journal_path = directory / "s7-server-requests.jsonl"

    @property
    def database_path(self):
        return self.directory / DATABASE_NAME

    def load_plan(self) -> S7ServerPlan:
        return _load_database_plan(self, decode_s7_plan, encode_s7_plan, S7ServerPlan)

    def save_plan(self, plan: S7ServerPlan):
        _save_database_plan(self, plan, encode_s7_plan)


def _load_database_plan(storage, decode, encode, default):
    with connect(storage.database_path) as connection:
        record = read_plan(connection, storage._journal_protocol)
        if record is not None:
            return decode(record)
        marker = "migrated:" + storage.plan_path.name
        if get_meta(connection, marker) is None:
            try:
                record = json.loads(storage.plan_path.read_bytes())
            except FileNotFoundError:
                record = encode(default())
            plan = decode(record)
            write_plan(connection, encode(plan))
            set_meta(connection, marker, "1")
            return plan
        return default()


def _save_database_plan(storage, plan, encode):
    plan.validate()
    with connect(storage.database_path) as connection:
        write_plan(connection, encode(plan))
        set_meta(connection, "migrated:" + storage.plan_path.name, "1")
