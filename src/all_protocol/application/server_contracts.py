from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .contracts import ErrorKind, Value
from .faults import FaultHit, FaultOperation, FaultUpdate
from .simulation import SimulationPoint, SimulationUpdate, WriteSpan


class ListenerState(StrEnum):
    STOPPED = "已停止"
    STARTING = "启动中"
    LISTENING = "监听中"
    STOPPING = "停止中"


@dataclass(frozen=True, slots=True)
class ListenerUpdate:
    state: ListenerState
    error: str = ""


class LifecycleAction(StrEnum):
    STARTED = "started"
    START_FAILED = "start_failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ServerLifecycle:
    action: LifecycleAction
    result: str


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    peer: str
    result: str
    rx: bytes = b""
    tx: bytes = b""
    details: tuple[tuple[str, Value | None], ...] = ()
    elapsed_ms: int | None = None
    error: ErrorKind | None = None
    session_id: str = ""
    request_id: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeChange:
    details: object


@dataclass(frozen=True, slots=True)
class ServerEvent:
    protocol: str
    endpoint: str
    time: datetime
    payload: ListenerUpdate | ServerLifecycle | RequestMetadata | RuntimeChange | SimulationUpdate | FaultUpdate


@dataclass(frozen=True, slots=True)
class ServerCallbacks:
    """Adapters emit on the listener's event loop, through the end of shutdown."""

    request: Callable[[datetime, RequestMetadata], None]
    changed: Callable[[object], None]
    written: Callable[[WriteSpan], None] | None = None
    fault: Callable[[FaultOperation], FaultHit | None] | None = None


class ServerAdapter(Protocol):
    """One listener. Construction and validation do not acquire transport resources."""

    @property
    def endpoint(self) -> str: ...
    def validate_plan(self) -> None: ...
    async def bind(self) -> None: ...
    async def shutdown(self) -> None:
        """Release resources even after a partial bind. Repeated calls are safe."""
        ...
    def read_current(self, target: object) -> Value: ...
    def write_current(self, target: object, value: Value) -> None: ...


class ServerCapability(StrEnum):
    READ_CURRENT = "read_current"
    WRITE_CURRENT = "write_current"


@dataclass(frozen=True, slots=True)
class ServerRegistration:
    key: str
    label: str
    create_adapter: Callable[[object, ServerCallbacks], ServerAdapter]
    capabilities: frozenset[ServerCapability]
    simulation_points: Callable[[object], Iterable[SimulationPoint]] | None = None
