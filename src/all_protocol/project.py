"""Portable project configuration; runtime logs remain in local storage."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
from uuid import uuid4

from .adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig, data_types
from .adapters.modbus_tcp.server_model import ServerPlan
from .adapters.modbus_tcp.values import DataType, RegisterOrder
from .adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Config, S7Target
from .adapters.s7.server_model import S7ServerPlan
from .application.contracts import ClientError
from .storage import (
    ConnectionProfile, LocalHistory, ServerStorage, S7ServerStorage,
    _load_profile, _locked, _replace_file, decode_modbus_plan, decode_s7_plan,
)


PROJECT_LIMIT_BYTES = 10 * 1024 * 1024


def validate_profile(profile: ConnectionProfile):
    """Validate without Qt forms, including types that numeric comparisons accept."""
    fields = profile.fields
    common = ("port", "timeout_ms", "offset", "count")
    extra = ("unit_id",) if profile.protocol == "modbus-tcp" else ("rack", "slot", "db", "bit")
    if not isinstance(fields.get("host"), str) or any(type(fields.get(key)) is not int for key in common + extra):
        raise ValueError("设备字段类型无效")
    if profile.protocol == "modbus-tcp":
        config = ModbusTcpConfig(fields["host"], fields["port"], fields["timeout_ms"])
        area = Area(fields["area"])
        target = ModbusTarget(fields["unit_id"], area, fields["offset"], fields["count"],
                              DataType(fields.get("data_type", data_types(area)[0])),
                              RegisterOrder(fields.get("order", RegisterOrder.BIG)))
    elif profile.protocol == "s7":
        config = S7Config(fields["host"], fields["port"], fields["rack"], fields["slot"], fields["timeout_ms"])
        target = S7Target(S7Area[fields["area"]], fields["db"], fields["offset"],
                          S7DataType(fields["data_type"]), fields["count"], fields["bit"])
    else:
        raise ValueError(f"不支持的设备协议：{profile.protocol}")
    config.validate()
    target.validate()
    if config.endpoint != profile.endpoint:
        raise ValueError("设备端点与连接字段不一致")


@dataclass(frozen=True)
class ProjectDevice:
    id: str
    name: str
    profile: ConnectionProfile


@dataclass(frozen=True)
class Project:
    name: str
    devices: tuple[ProjectDevice, ...] = ()
    modbus: ServerPlan = field(default_factory=ServerPlan)
    s7: S7ServerPlan = field(default_factory=S7ServerPlan)

    def remember(self, profile: ConnectionProfile) -> "Project":
        validate_profile(profile)
        previous = next((item for item in self.devices if item.profile.key == profile.key), None)
        device = (replace(previous, profile=deepcopy(profile)) if previous else
                  ProjectDevice(str(uuid4()), profile.endpoint, deepcopy(profile)))
        if previous is not None and previous.profile == profile:
            return self
        return replace(self, devices=(device,) + tuple(item for item in self.devices if item.id != device.id))


def project_record(project: Project) -> dict:
    modbus = {"schema_version": 2, "protocol": "modbus-tcp", **asdict(project.modbus)}
    modbus["stations"] = [
        {**asdict(station), "points": [asdict(point) for point in station.points]}
        for station in project.modbus.stations
    ]
    s7 = {"schema_version": 1, "protocol": "s7", **asdict(project.s7)}
    s7["points"] = [asdict(point) for point in project.s7.points]
    return {
        "format": "all-protocol-project", "schema_version": 1, "name": project.name,
        "devices": [asdict(item) for item in project.devices],
        "servers": {"modbus-tcp": modbus, "s7": s7},
    }


def decode_project(data) -> Project:
    try:
        if (not isinstance(data, dict) or data.get("format") != "all-protocol-project"
                or type(data.get("schema_version")) is not int or data["schema_version"] != 1):
            raise ValueError("项目格式或版本无法识别")
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("项目名称必须为 1–200 字符")
        if not isinstance(data["devices"], list):
            raise ValueError("devices 必须是列表")
        devices, ids, endpoints = [], set(), set()
        for index, item in enumerate(data["devices"]):
            try:
                if not isinstance(item, dict):
                    raise ValueError("设备必须是对象")
                if any(not isinstance(item[key], str) or not item[key].strip() for key in ("id", "name")):
                    raise ValueError("设备 ID 与名称不能为空")
                profile = _load_profile(item["profile"])
                if profile is None:
                    raise ValueError("设备配置无效")
                validate_profile(profile)
                if item["id"] in ids or profile.key in endpoints:
                    raise ValueError("设备 ID 或协议端点重复")
                ids.add(item["id"])
                endpoints.add(profile.key)
                devices.append(ProjectDevice(item["id"], item["name"], deepcopy(profile)))
            except (KeyError, TypeError, ValueError, ClientError) as error:
                raise ValueError(f"devices[{index}]：{error}") from error
        servers = data["servers"]
        if not isinstance(servers, dict) or set(servers) != {"modbus-tcp", "s7"}:
            raise ValueError("servers 必须包含且仅包含 modbus-tcp 和 s7")
        plans = []
        for protocol, decode in (("modbus-tcp", decode_modbus_plan), ("s7", decode_s7_plan)):
            try:
                plans.append(decode(servers[protocol]))
            except (KeyError, TypeError, ValueError, ClientError) as error:
                raise ValueError(f"servers.{protocol}：{error}") from error
        return Project(name, tuple(devices), *plans)
    except (KeyError, TypeError, ClientError) as error:
        raise ValueError(f"项目字段无效：{error}") from error


def _reject_constant(value):
    raise ValueError(f"项目不允许非有限数值：{value}")


def _read_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        contents = stream.read(PROJECT_LIMIT_BYTES + 1)
    if len(contents) > PROJECT_LIMIT_BYTES:
        raise ValueError("项目文件超过 10 MiB")
    return contents


class ProjectFile:
    """Tracks an opened file revision to prevent accidental external overwrites."""

    def __init__(self):
        self.path: Path | None = None
        self._contents: bytes | None = None
        self.saved: Project | None = None

    def open(self, path: Path) -> Project:
        path = path.resolve()
        contents = _read_bytes(path)
        try:
            project = decode_project(json.loads(contents, parse_constant=_reject_constant))
        except (UnicodeError, RecursionError) as error:
            raise ValueError("项目文件不是有效的 UTF-8 JSON") from error
        self.path, self._contents, self.saved = path, contents, deepcopy(project)
        return project

    def save(self, project: Project, path: Path):
        path = path.resolve()
        record = project_record(project)
        # Also validate in-memory configuration before touching the destination.
        contents = (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        decode_project(json.loads(contents))
        if len(contents) > PROJECT_LIMIT_BYTES:
            raise ValueError("项目文件超过 10 MiB")
        with _locked(path.parent, path.name + ".lock"):
            if path == self.path:
                current = _read_bytes(path) if path.exists() else None
                if current != self._contents:
                    raise ValueError("项目文件已被其他程序修改或删除，请重新打开或另存为")
            _replace_file(path, contents)
        self.path, self._contents, self.saved = path, contents, deepcopy(project)


@dataclass
class ProjectWorkspace:
    project: Project
    file: ProjectFile = field(default_factory=ProjectFile)

    @classmethod
    def from_local(cls, name: str, directory: Path) -> "ProjectWorkspace":
        history = LocalHistory(directory)
        # Import must not silently drop malformed history entries.
        try:
            raw = json.loads(history.profiles_path.read_bytes())
        except FileNotFoundError:
            raw = []
        if not isinstance(raw, list):
            raise ValueError("本地连接配置不是列表")
        project = Project(name, modbus=ServerStorage(directory).load_plan(), s7=S7ServerStorage(directory).load_plan())
        for item in reversed(raw):
            profile = _load_profile(item)
            if profile is None:
                raise ValueError("本地连接配置包含无效记录")
            project = project.remember(profile)
        project = decode_project(project_record(project))
        return cls(project)


class ProjectHistory:
    """Project profiles with the existing local journal, never local profiles."""

    project_mode = True

    def __init__(self, workspace: ProjectWorkspace, local: LocalHistory):
        self.workspace, self.local = workspace, local

    def profiles(self):
        return tuple(deepcopy(item.profile) for item in self.workspace.project.devices)

    def remember(self, profile):
        self.workspace.project = self.workspace.project.remember(profile)
        return self.profiles()

    def forget(self, profile):
        project = self.workspace.project
        self.workspace.project = replace(project, devices=tuple(item for item in project.devices if item.profile.key != profile.key))
        return self.profiles()

    def recent_entries(self):
        return self.local.recent_entries()

    def append_entry(self, entry):
        return self.local.append_entry(entry)


class ProjectServerStorage:
    def __init__(self, workspace: ProjectWorkspace, protocol: str, local):
        self.workspace, self.protocol, self.local = workspace, protocol, local
        self.plan_path = "当前项目"
        self.project_mode = True

    def load_plan(self):
        return self.workspace.project.modbus if self.protocol == "modbus-tcp" else self.workspace.project.s7

    def save_plan(self, plan):
        plan.validate()
        key = "modbus" if self.protocol == "modbus-tcp" else "s7"
        self.workspace.project = replace(self.workspace.project, **{key: plan})

    def recent_requests(self):
        return self.local.recent_requests()

    def append_request(self, request):
        return self.local.append_request(request)
