import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..adapters.modbus_tcp.model import Area
from ..adapters.modbus_tcp.server import ServerRequest, TcpServer
from ..adapters.modbus_tcp.server_model import ServerPlan


class ListenerState(StrEnum):
    STOPPED = "已停止"
    STARTING = "启动中"
    LISTENING = "监听中"
    STOPPING = "停止中"


@dataclass(frozen=True, slots=True)
class ListenerUpdate:
    state: ListenerState
    error: str = ""


class ServerSession:
    def __init__(self, emit):
        self.emit = emit
        self.server: TcpServer | None = None
        self._lifecycle = asyncio.Lock()

    async def start(self, plan: ServerPlan):
        async with self._lifecycle:
            if self.server is not None:
                return
            self.emit(ListenerUpdate(ListenerState.STARTING))
            server = None
            try:
                plan.validate(starting=True)
                server = TcpServer(plan, self.emit, self.emit)
                await server.bind()
            except (ValueError, OSError) as error:
                if server is not None:
                    await server.shutdown()
                detail = f"启动失败 · {plan.host}:{plan.port} · {error}"
                self._record(detail)
                self.emit(ListenerUpdate(ListenerState.STOPPED, detail))
                return
            self.server = server
            self._record(f"启动监听 · {plan.host}:{plan.port}")
            self.emit(ListenerUpdate(ListenerState.LISTENING))

    async def stop(self):
        async with self._lifecycle:
            if self.server is None:
                return
            self.emit(ListenerUpdate(ListenerState.STOPPING))
            await self.server.shutdown()
            self.server = None
            self._record("已停止监听")
            self.emit(ListenerUpdate(ListenerState.STOPPED))

    async def set_current(self, unit: int, area: Area, offset: int, value: int):
        if self.server is not None:
            try:
                self.server.set_current(unit, area, offset, value)
            except ValueError as error:
                self.emit(ListenerUpdate(ListenerState.LISTENING, str(error)))

    def _record(self, result: str):
        self.emit(ServerRequest(datetime.now(), "", None, None, None, None, result))
