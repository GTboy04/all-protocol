from ...application.contracts import Value
from ...application.server_contracts import RequestMetadata, ServerCallbacks
from ...application.simulation import WriteSpan
from .server import ServerRequest, TcpServer, ValuesChanged
from .server_model import ModbusServerTarget, ServerPlan


class ModbusServerAdapter:
    def __init__(self, plan: object, callbacks: ServerCallbacks):
        if not isinstance(plan, ServerPlan):
            raise ValueError("需要 Modbus TCP 服务端方案")
        self._plan = plan
        self._callbacks = callbacks
        self._server: TcpServer | None = None

    @property
    def endpoint(self) -> str:
        host = str(self._plan.host)
        return f"[{host}]:{self._plan.port}" if ":" in host else f"{host}:{self._plan.port}"

    def validate_plan(self):
        self._plan.validate(starting=True)

    def _record(self, request: ServerRequest):
        details = (("unit_id", request.unit_id), ("function", request.function),
                   ("offset", request.offset), ("count", request.count))
        self._callbacks.request(request.time, RequestMetadata(request.peer, request.result, request.rx, request.tx,
                                                             details, request.elapsed_ms, request.error))

    def _changed(self, change: ValuesChanged):
        self._callbacks.changed(change)
        if self._callbacks.written is not None:
            self._callbacks.written(WriteSpan((change.unit_id, change.area), change.offset,
                                              change.offset + len(change.values)))

    async def bind(self):
        self._server = TcpServer(self._plan, self._changed, self._record, self._callbacks.fault)
        await self._server.bind()

    async def shutdown(self):
        if self._server is not None:
            await self._server.shutdown()
            self._server = None

    def read_current(self, target: object) -> Value:
        if not isinstance(target, ModbusServerTarget) or self._server is None:
            raise ValueError("需要正在监听的 Modbus TCP 点位")
        return self._server.read_current(target.unit_id, target.area, target.offset)

    def write_current(self, target: object, value: Value):
        if not isinstance(target, ModbusServerTarget) or self._server is None:
            raise ValueError("需要正在监听的 Modbus TCP 点位")
        self._server.set_current(target.unit_id, target.area, target.offset, value)
