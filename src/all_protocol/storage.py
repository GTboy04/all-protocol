import json
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from os import SEEK_END
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

from .adapters.modbus_tcp.model import Area
from .adapters.modbus_tcp.server import ServerRequest
from .adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from .application.contracts import ErrorKind, JournalEntry


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


def _load_server_request(line: bytes) -> ServerRequest:
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("Expected a server request")
    for field in ("peer", "result"):
        if not isinstance(data[field], str):
            raise ValueError(f"Invalid request field {field}")
    for field in ("unit_id", "function", "offset", "count"):
        if data[field] is not None and type(data[field]) is not int:
            raise ValueError(f"Invalid request field {field}")
    data["time"] = datetime.fromisoformat(data["time"])
    data["rx"] = bytes.fromhex(data["rx"])
    data["tx"] = bytes.fromhex(data["tx"])
    return ServerRequest(**data)


class ServerStorage:
    def __init__(self, directory: Path):
        self.directory = directory
        self.plan_path = directory / "modbus-tcp-server.json"
        self.journal_path = directory / "server-requests.jsonl"

    def load_plan(self) -> ServerPlan:
        try:
            data = json.loads(self.plan_path.read_bytes())
        except FileNotFoundError:
            return ServerPlan()
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data["schema_version"] != 1 or data.get("protocol") != "modbus-tcp"):
            raise ValueError("方案版本或协议无法识别")
        if not isinstance(data["stations"], list):
            raise ValueError("从站必须是列表")
        for station in data["stations"]:
            if not isinstance(station, dict) or not isinstance(station.get("points"), list):
                raise ValueError("从站点位必须是列表")
        stations = tuple(
            Station(station["unit_id"], station["name"], tuple(
                ServerPoint(Area(point["area"]), point["offset"], point["initial"], point["name"])
                for point in station["points"]
            ))
            for station in data["stations"]
        )
        plan = ServerPlan(data["host"], data["port"], stations)
        plan.validate()
        return plan

    def save_plan(self, plan: ServerPlan):
        plan.validate()
        record = {"schema_version": 1, "protocol": "modbus-tcp", **asdict(plan)}
        with _locked(self.directory, "server-plan.lock"):
            _replace_file(self.plan_path, _json_bytes(record))

    def recent_requests(self) -> tuple[ServerRequest, ...]:
        with _locked(self.directory, "server-journal.lock"):
            return _read_journal(self.journal_path, _load_server_request)

    def append_request(self, request: ServerRequest) -> bool:
        record = asdict(request)
        record.update(time=request.time.isoformat(), rx=request.rx.hex(), tx=request.tx.hex())
        with _locked(self.directory, "server-journal.lock"):
            return _append_journal(self.journal_path, record)
