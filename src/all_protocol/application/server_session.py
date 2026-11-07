import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..adapters.modbus_tcp.server import ServerRequest, TcpServer


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
    def __init__(self, emit, create_server=TcpServer, record_lifecycle=None):
        self.emit = emit
        self.server = None
        self._create_server = create_server
        self._record_lifecycle = record_lifecycle or self._modbus_lifecycle
        self._lifecycle = asyncio.Lock()

    async def start(self, plan):
        async with self._lifecycle:
            if self.server is not None:
                return
            self.emit(ListenerUpdate(ListenerState.STARTING))
            server = None
            try:
                plan.validate(starting=True)
                server = self._create_server(plan, self.emit, self.emit)
                await server.bind()
            except (ValueError, OSError) as error:
                if server is not None:
                    await server.shutdown()
                detail = f"启动失败 · {plan.host}:{plan.port} · {error}"
                self.emit(self._record_lifecycle(detail))
                self.emit(ListenerUpdate(ListenerState.STOPPED, detail))
                return
            self.server = server
            self.emit(self._record_lifecycle(f"启动监听 · {plan.host}:{plan.port}"))
            self.emit(ListenerUpdate(ListenerState.LISTENING))

    async def stop(self):
        async with self._lifecycle:
            if self.server is None:
                return
            self.emit(ListenerUpdate(ListenerState.STOPPING))
            await self.server.shutdown()
            self.server = None
            self.emit(self._record_lifecycle("已停止监听"))
            self.emit(ListenerUpdate(ListenerState.STOPPED))

    async def set_current(self, *args):
        if self.server is not None:
            try:
                self.server.set_current(*args)
            except ValueError as error:
                self.emit(ListenerUpdate(ListenerState.LISTENING, str(error)))

    @staticmethod
    def _modbus_lifecycle(result: str) -> ServerRequest:
        return ServerRequest(datetime.now(), "", None, None, None, None, result)
