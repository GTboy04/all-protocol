"""Portable project configuration; runtime logs remain in local storage."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
from uuid import uuid4

from .adapters.modbus_rtu.model import ModbusRtuConfig, ModbusRtuTarget
from .adapters.modbus_rtu.server_model import RtuServerPlan
from .adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig, data_types
from .adapters.modbus_tcp.server_model import ServerPlan
from .adapters.modbus_tcp.values import DataType, RegisterOrder
from .adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Config, S7Target
from .adapters.s7.server_model import S7ServerPlan
from .adapters.opcua.model import OpcUaConfig
from .adapters.opcua.server_model import UaServerPlan
from .application.contracts import ClientError
from .application.recovery import RecoveryPolicy, PREFIX as RECOVERY_PREFIX
from .point_tables import PointTable, decode_table, table_record, decode_target
from .database import SQLITE_HEADER, encode_project_database, decode_project_database
from .storage import (
    ConnectionProfile, LocalHistory, ServerStorage, S7ServerStorage,
    RtuServerStorage, decode_rtu_plan, encode_rtu_plan, _load_profile, _locked, _replace_file, decode_modbus_plan, decode_s7_plan, encode_modbus_plan, encode_s7_plan,
    UaServerStorage, decode_ua_plan, encode_ua_plan,
)


PROJECT_LIMIT_BYTES = 10 * 1024 * 1024


def validate_profile(profile: ConnectionProfile):
    """Validate without Qt forms, including types that numeric comparisons accept."""
    RecoveryPolicy.from_fields(profile.fields)
    fields = {key: value for key, value in profile.fields.items() if not key.startswith(RECOVERY_PREFIX)}
    if profile.protocol == "opcua":
        connection_keys = {"url", "security", "identity_id", "timeout_ms"}
        target_keys = {"namespace_uri", "identifier_type", "identifier", "data_type", "count"}
        if set(fields) != connection_keys | target_keys:
            raise ValueError("OPC UA 配置字段无效，项目不得含凭据或本机证书路径")
        config = OpcUaConfig(**{key: fields[key] for key in connection_keys})
        config.validate()
        decode_target("opcua", {key: fields[key] for key in target_keys})
        if config.endpoint != profile.endpoint:
            raise ValueError("设备端点与连接字段不一致")
        return config
    rtu = profile.protocol == "modbus-rtu"
    common = ("timeout_ms", "offset", "count") + (("baudrate", "stopbits") if rtu else ("port",))
    extra = ("unit_id",) if profile.protocol in ("modbus-tcp", "modbus-rtu") else ("rack", "slot", "db", "bit")
    if not isinstance(fields.get("serial_port" if rtu else "host"), str) or any(type(fields.get(key)) is not int for key in common + extra):
        raise ValueError("设备字段类型无效")
    if profile.protocol in ("modbus-tcp", "modbus-rtu"):
        config = (ModbusRtuConfig(*(fields[key] for key in ("serial_port", "baudrate", "parity", "stopbits", "timeout_ms")))
                  if rtu else ModbusTcpConfig(fields["host"], fields["port"], fields["timeout_ms"]))
        area = Area(fields["area"])
        target_type = ModbusRtuTarget if rtu else ModbusTarget
        target = target_type(fields["unit_id"], area, fields["offset"], fields["count"],
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
    return config


@dataclass(frozen=True)
class ProjectDevice:
    id: str
    name: str
    profile: ConnectionProfile


SERVER_FIELDS = {"modbus-tcp": "modbus", "s7": "s7", "modbus-rtu": "rtu", "opcua": "ua"}
SERVER_NAMES = {"modbus-tcp": "Modbus TCP", "s7": "Siemens S7", "modbus-rtu": "Modbus RTU", "opcua": "OPC UA（本机模拟）"}
SERVER_CODECS = {"modbus-tcp": (encode_modbus_plan, decode_modbus_plan), "s7": (encode_s7_plan, decode_s7_plan),
                 "modbus-rtu": (encode_rtu_plan, decode_rtu_plan), "opcua": (encode_ua_plan, decode_ua_plan)}


@dataclass(frozen=True)
class ProjectSimulator:
    id: str
    name: str
    protocol: str
    plan: ServerPlan | S7ServerPlan | RtuServerPlan | UaServerPlan


def project_simulators(project):
    if project.simulators is not None:
        return project.simulators
    return tuple(ProjectSimulator(key, SERVER_NAMES[key], key, getattr(project, field))
                 for key, field in SERVER_FIELDS.items())


@dataclass(frozen=True)
class WorkspaceLayout:
    """Splitter positions in thousandths; no window coordinates or runtime state."""

    navigation: int = 220
    journal: int = 290
    journal_visible: bool = True
    selection: tuple[str, str] = ("", "")
    server_values: bool = False
    navigation_visible: bool = True

    @classmethod
    def decode(cls, raw):
        if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
            return None
        navigation, journal = raw.get("navigation"), raw.get("journal")
        visible, values = raw.get("journal_visible"), raw.get("server_values")
        navigation_visible = raw.get("navigation_visible")
        selection = raw.get("selection")
        if (any(type(size) is not int or not 100 <= size <= 850 for size in (navigation, journal))
                or any(type(flag) is not bool for flag in (visible, values, navigation_visible))
                or not isinstance(selection, list) or len(selection) != 2
                or not all(isinstance(part, str) for part in selection)
                or selection[0] not in ("", "device", "points", "table", "server")):
            return None
        return cls(navigation, journal, visible, tuple(selection), values, navigation_visible)


@dataclass(frozen=True)
class Project:
    name: str
    devices: tuple[ProjectDevice, ...] = ()
    modbus: ServerPlan = field(default_factory=ServerPlan)
    s7: S7ServerPlan = field(default_factory=S7ServerPlan)
    point_tables: tuple[PointTable, ...] = ()
    layout: WorkspaceLayout | None = None
    rtu: RtuServerPlan = field(default_factory=RtuServerPlan)
    ua: UaServerPlan = field(default_factory=UaServerPlan)
    simulators: tuple[ProjectSimulator, ...] | None = None

    def remember(self, profile: ConnectionProfile) -> "Project":
        validate_profile(profile)
        previous = next((item for item in self.devices if item.profile.key == profile.key), None)
        device = (replace(previous, profile=deepcopy(profile)) if previous else
                  ProjectDevice(str(uuid4()), profile.endpoint, deepcopy(profile)))
        if previous is not None and previous.profile == profile:
            return self
        return replace(self, devices=(device,) + tuple(item for item in self.devices if item.id != device.id))

    def with_table(self, table: PointTable) -> "Project":
        exists = any(item.id == table.id for item in self.point_tables)
        tables = tuple(table if item.id == table.id else item for item in self.point_tables)
        project = replace(self, point_tables=tables if exists else tables + (table,))
        decode_project(project_record(project))
        return project

    def without_table(self, table_id: str) -> "Project":
        return replace(self, point_tables=tuple(item for item in self.point_tables if item.id != table_id))


def project_record(project: Project) -> dict:
    ua = project.ua != UaServerPlan() or any(device.profile.protocol == "opcua" for device in project.devices)
    recovery = any(RecoveryPolicy.from_fields(device.profile.fields) != RecoveryPolicy() for device in project.devices)
    bits = (any(point.bit_definitions for table in project.point_tables for point in table.points)
            or any(point.bit_definitions for station in (*project.modbus.stations, *project.rtu.stations) for point in station.points)
            or any(point.bit_definitions for point in (*project.s7.points, *project.ua.points)))
    extensions = any(station.extensions for station in project.modbus.stations) or any(
        item.protocol == "modbus-tcp" and any(station.extensions for station in item.plan.stations)
        for item in project.simulators or ())
    ua = ua or recovery or bits or project.simulators is not None or extensions
    record = {
        "format": "all-protocol-project", "schema_version": 9 if extensions else 8 if project.simulators is not None else 7 if bits else 6 if recovery else 5 if ua else 4, "name": project.name,
        "devices": [asdict(item) for item in project.devices],
        "servers": {"modbus-tcp": encode_modbus_plan(project.modbus), "s7": encode_s7_plan(project.s7),
                    "modbus-rtu": encode_rtu_plan(project.rtu)},
        "point_tables": [table_record(table) for table in project.point_tables],
    }
    if ua:
        record["servers"]["opcua"] = encode_ua_plan(project.ua)
    if project.simulators is not None:
        record["simulators"] = [dict(id=item.id, name=item.name, protocol=item.protocol,
                                     plan=SERVER_CODECS[item.protocol][0](item.plan)) for item in project.simulators]
    if (record["servers"]["modbus-tcp"]["schema_version"] >= 6 or any(
            item["protocol"] == "modbus-tcp" and item["plan"]["schema_version"] >= 6
            for item in record.get("simulators", []))):
        record["schema_version"] = 10
    if project.layout is not None:
        record["workspace_layout"] = {"version": 1, **asdict(project.layout), "selection": list(project.layout.selection)}
    return record


def decode_project(data) -> Project:
    try:
        if (not isinstance(data, dict) or data.get("format") != "all-protocol-project"
                or type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)):
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
                if RecoveryPolicy.from_fields(profile.fields) != RecoveryPolicy() and data["schema_version"] < 6:
                    raise ValueError("恢复配置须使用项目 v6")
                if profile.protocol == "opcua" and data["schema_version"] < 5:
                    raise ValueError("OPC UA 设备须使用项目 v5")
                if item["id"] in ids or profile.key in endpoints:
                    raise ValueError("设备 ID 或协议端点重复")
                ids.add(item["id"])
                endpoints.add(profile.key)
                devices.append(ProjectDevice(item["id"], item["name"], deepcopy(profile)))
            except (KeyError, TypeError, ValueError, ClientError) as error:
                raise ValueError(f"devices[{index}]：{error}") from error
        servers = data["servers"]
        expected = {"modbus-tcp", "s7", "modbus-rtu"} if data["schema_version"] >= 4 else {"modbus-tcp", "s7"}
        if data["schema_version"] >= 5:
            expected.add("opcua")
        if not isinstance(servers, dict) or set(servers) != expected:
            raise ValueError(f"servers 必须包含且仅包含 {', '.join(sorted(expected))}")
        plans = []
        for protocol, decode in (("modbus-tcp", decode_modbus_plan), ("s7", decode_s7_plan)):
            try:
                plans.append(decode(servers[protocol]))
            except (KeyError, TypeError, ValueError, ClientError) as error:
                raise ValueError(f"servers.{protocol}：{error}") from error
        if data["schema_version"] < 9 and any(station.extensions for station in plans[0].stations):
            raise ValueError("场景扩展须使用项目 v9")
        if data["schema_version"] < 10 and servers["modbus-tcp"]["schema_version"] >= 6:
            raise ValueError("点位取值或无条件规则须使用项目 v10")
        tables, table_ids, point_ids = [], set(), set()
        if data["schema_version"] == 1:
            if data.get("point_tables"):
                raise ValueError("v1 项目不允许包含点表，请使用 v2 格式")
            raw_tables = []
        else:
            raw_tables = data["point_tables"]
        if not isinstance(raw_tables, list):
            raise ValueError("point_tables 必须是列表")
        devices_by_id = {item.id: item for item in devices}
        for index, raw_table in enumerate(raw_tables):
            try:
                device = devices_by_id[raw_table["device_id"]]
                if data["schema_version"] >= 3 and not {"interval_ms", "stale_after_ms"}.issubset(raw_table):
                    raise ValueError("v3 点表缺少轮询周期或过期时间")
                table = decode_table(raw_table, device.profile.protocol)
                if table.id in table_ids or any(point.id in point_ids for point in table.points):
                    raise ValueError("点表或点位 ID 重复")
                table_ids.add(table.id)
                point_ids.update(point.id for point in table.points)
                tables.append(table)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"point_tables[{index}]：{error}") from error
        simulators = None
        if data["schema_version"] >= 8 and ("simulators" in data or data["schema_version"] == 8):
            raw = data["simulators"]
            if not isinstance(raw, list) or len(raw) > 64:
                raise ValueError("模拟设备必须是列表，最多 64 个服务")
            simulators, seen = [], set()
            for item in raw:
                if (not isinstance(item, dict) or set(item) != {"id", "name", "protocol", "plan"}
                        or any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 200
                               for key in ("id", "name"))
                        or item["id"] in seen or item["protocol"] not in SERVER_CODECS):
                    raise ValueError("模拟设备 ID、名称或协议无效，ID 不得重复")
                plan = SERVER_CODECS[item["protocol"]][1](item["plan"])
                if data["schema_version"] < 9 and item["protocol"] == "modbus-tcp" and any(s.extensions for s in plan.stations):
                    raise ValueError("场景扩展须使用项目 v9")
                if data["schema_version"] < 10 and item["protocol"] == "modbus-tcp" and item["plan"]["schema_version"] >= 6:
                    raise ValueError("点位取值或无条件规则须使用项目 v10")
                simulators.append(ProjectSimulator(item["id"], item["name"], item["protocol"], plan))
                seen.add(item["id"])
            simulators = tuple(simulators)
        elif "simulators" in data:
            raise ValueError("多个模拟设备须使用项目 v8")
        return Project(name, tuple(devices), *plans, tuple(tables), WorkspaceLayout.decode(data.get("workspace_layout")),
                       decode_rtu_plan(servers["modbus-rtu"]) if data["schema_version"] >= 4 else RtuServerPlan(),
                       decode_ua_plan(servers["opcua"]) if data["schema_version"] >= 5 else UaServerPlan(), simulators)
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
            record = (decode_project_database(contents) if contents.startswith(SQLITE_HEADER) else
                      json.loads(contents, parse_constant=_reject_constant))
            project = decode_project(record)
        except (UnicodeError, RecursionError) as error:
            raise ValueError("项目文件不是有效的 UTF-8 JSON") from error
        self.path, self._contents, self.saved = path, contents, deepcopy(project)
        return project

    def save(self, project: Project, path: Path):
        path = path.resolve()
        record = project_record(project)
        # Also validate in-memory configuration before touching the destination.
        decode_project(json.loads(json.dumps(record, allow_nan=False)))
        contents = encode_project_database(record)
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
        profiles = history.profiles()
        project = Project(name, modbus=ServerStorage(directory).load_plan(), s7=S7ServerStorage(directory).load_plan(),
                          rtu=RtuServerStorage(directory).load_plan(), ua=UaServerStorage(directory).load_plan())
        for profile in reversed(profiles):
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
        removed = {item.id for item in project.devices if item.profile.key == profile.key}
        self.workspace.project = replace(project, devices=tuple(item for item in project.devices if item.id not in removed),
                                         point_tables=tuple(table for table in project.point_tables if table.device_id not in removed))
        return self.profiles()

    def related_table_count(self, profile):
        device = next((item for item in self.workspace.project.devices if item.profile.key == profile.key), None)
        return sum(table.device_id == device.id for table in self.workspace.project.point_tables) if device else 0

    def recent_entries(self):
        return self.local.recent_entries()

    def read_log(self):
        return self.local.read_log()

    def clear_log(self, **scope):
        self.local.clear_log(**scope)

    def append_entry(self, entry):
        return self.local.append_entry(entry)


class ProjectServerStorage:
    def __init__(self, workspace: ProjectWorkspace, project_field: str, local, simulator=None):
        self.workspace, self.project_field, self.local = workspace, project_field, local
        self.plan_path = "当前项目"
        self.project_mode = True
        self.simulator_id = simulator.id if simulator else None
        self.simulator_name = simulator.name if simulator else None
        self.simulator_protocol = simulator.protocol if simulator else None

    def load_plan(self):
        if self.simulator_id is not None:
            return next(item.plan for item in project_simulators(self.workspace.project) if item.id == self.simulator_id)
        return getattr(self.workspace.project, self.project_field)

    def save_plan(self, plan):
        plan.validate()
        if self.simulator_id is not None and self.workspace.project.simulators is not None:
            self.workspace.project = replace(self.workspace.project, simulators=tuple(
                replace(item, plan=plan) if item.id == self.simulator_id else item
                for item in self.workspace.project.simulators))
            return
        self.workspace.project = replace(self.workspace.project, **{self.project_field: plan})

    def recent_events(self):
        # A local protocol journal may contain other projects and simulator instances.
        return ()

    def clear_log(self, *, endpoint):
        self.local.clear_log(endpoint=endpoint)

    def append_event(self, event):
        return self.local.append_event(event)

    def append_events(self, events):
        return self.local.append_events(events)
