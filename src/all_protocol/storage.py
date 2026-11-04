import json
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from os import SEEK_END
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

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


class LocalHistory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.profiles_path = directory / "connections.json"
        self.journal_path = directory / "requests.jsonl"

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.directory / "history.lock"))
        if not lock.tryLock(250):
            raise OSError(f"无法锁定本地历史目录（{lock.error().name}）")
        try:
            yield
        finally:
            lock.unlock()

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

    def remember(self, profile: ConnectionProfile) -> tuple[ConnectionProfile, ...]:
        with self._locked():
            profiles = self._read_profiles()
            if profiles and profiles[0] == profile:
                return profiles
            updated = (profile,) + tuple(item for item in profiles
                                        if (item.protocol, item.endpoint) != (profile.protocol, profile.endpoint))
            contents = json.dumps([asdict(item) for item in updated], ensure_ascii=False, indent=2)
            _replace_file(self.profiles_path, (contents + "\n").encode("utf-8"))
            return updated

    def recent_entries(self) -> tuple[JournalEntry, ...]:
        with self._locked():
            if not self.journal_path.exists():
                return ()
            if self.journal_path.stat().st_size > LOG_LIMIT_BYTES:
                self.journal_path.unlink()
                return ()
            recent = deque(maxlen=VISIBLE_LOG_ROWS)
            with self.journal_path.open("rb") as journal:
                for line in journal:
                    try:
                        recent.append(_load_entry(line))
                    except (ValueError, TypeError, KeyError):
                        continue
            return tuple(reversed(recent))

    def append_entry(self, entry: JournalEntry) -> bool:
        """Append an entry and report whether the previous journal was cleared."""
        record = asdict(entry)
        record["time"] = entry.time.isoformat()
        record["packets"] = [(sending, packet.hex()) for sending, packet in entry.packets]
        line = _json_bytes(record)
        if len(line) > LOG_LIMIT_BYTES:
            raise OSError("单条日志超过 5 MB")
        with self._locked():
            with self.journal_path.open("a+b") as journal:
                size = journal.seek(0, SEEK_END)
                separator = b""
                if size:
                    journal.seek(-1, SEEK_END)
                    if journal.read(1) != b"\n":
                        separator = b"\n"
                if size + len(separator) + len(line) <= LOG_LIMIT_BYTES:
                    journal.write(separator + line)
                    return False
            _replace_file(self.journal_path, line)
            return True
