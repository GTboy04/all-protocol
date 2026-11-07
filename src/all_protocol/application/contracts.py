from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ErrorKind(StrEnum):
    INPUT = "输入错误"
    CONNECT = "连接失败"
    TIMEOUT = "请求超时"
    DISCONNECTED = "连接中断"
    PROTOCOL = "协议异常"
    RESPONSE = "响应无效"
    INTERNAL = "内部错误"


class ClientError(Exception):
    def __init__(self, kind: ErrorKind, detail: str, code: int | None = None, *, write_rejected: bool = False):
        super().__init__(detail)
        self.kind = kind
        self.code = code
        self.write_rejected = write_rejected


class ConnectionState(StrEnum):
    OFFLINE = "未连接"
    CONNECTING = "连接中"
    ONLINE = "已连接"
    CLOSING = "断开中"


class Target(Protocol):
    @property
    def label(self) -> str: ...


type Value = int | bool | float | str


@dataclass(frozen=True, slots=True)
class Command:
    target: Target
    values: tuple[Value, ...] | None = None

    @property
    def operation(self) -> str:
        return "读取" if self.values is None else "写入"


@dataclass(frozen=True, slots=True)
class Point:
    address: str
    label: str
    value: Value
    hex_value: str = ""


@dataclass(frozen=True, slots=True)
class SessionState:
    connection: ConnectionState = ConnectionState.OFFLINE
    endpoint: str = ""
    busy: bool = False
    polling: bool = False
    generation: int = 0
    queued: int = 0
    queue_capacity: int = 32
    polling_groups: tuple[str, ...] = ()
    manual_tables: tuple[str, ...] = ()
    device_id: str = ""
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    request_id: int
    time: datetime
    operation: str
    target: str
    function: str
    elapsed_ms: int
    outcome: str
    error: ErrorKind | None = None
    detail: str = ""
    packets: tuple[tuple[bool, bytes], ...] = ()
    protocol: str = ""
    endpoint: str = ""
    operation_id: int | None = None
    device_id: str = ""
    session_id: str = ""
    generation: int = 0


@dataclass(frozen=True, slots=True)
class ReadUpdate:
    command: Command
    entry: JournalEntry
    points: tuple[Point, ...] | None
    generation: int = 0


@dataclass(frozen=True, slots=True)
class TableReadUpdate:
    table_id: str
    point_id: str
    update: ReadUpdate


@dataclass(frozen=True, slots=True)
class OperationRejected:
    operation: str
    detail: str
    generation: int
    table_id: str = ""


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    device_id: str
    session_id: str
    generation: int
    runtime_id: str
    payload: object
    config: object | None = None


@dataclass(frozen=True, slots=True)
class DeviceRemoved:
    pass


@dataclass(frozen=True, slots=True)
class DeviceResult:
    device_id: str
    successful: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BatchResult:
    operation: str
    results: tuple[DeviceResult, ...]


class Client(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    def function(self, command: Command) -> str: ...
    async def read(self, target: Target) -> tuple[Point, ...]: ...


class WritableClient(Client, Protocol):
    async def write(self, target: Target, values: tuple[Value, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class ClientEvents:
    packet: Callable[[bool, bytes], None]
    disconnected: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ProtocolRegistration:
    key: str
    label: str
    create_client: Callable[[object, ClientEvents], Client]
    writable: bool = False
