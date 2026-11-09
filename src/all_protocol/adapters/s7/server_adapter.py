from ...application.contracts import Value
from ...application.server_contracts import RequestMetadata, ServerCallbacks
from ...application.simulation import WriteSpan
from .model import S7Target
from .server import S7Server, S7ServerRequest
from .server_model import S7ServerPlan, S7ValuesChanged


class S7ServerAdapter:
    def __init__(self, plan: object, callbacks: ServerCallbacks):
        if not isinstance(plan, S7ServerPlan):
            raise ValueError("需要 Siemens S7 服务端方案")
        self._plan = plan
        self._callbacks = callbacks
        self._server: S7Server | None = None

    @property
    def endpoint(self) -> str:
        host = str(self._plan.host)
        return f"[{host}]:{self._plan.port}" if ":" in host else f"{host}:{self._plan.port}"

    def validate_plan(self):
        self._plan.validate(starting=True)

    def _record(self, request: S7ServerRequest):
        details = (("function", request.function), ("target", request.target))
        self._callbacks.request(request.time, RequestMetadata(request.peer, request.result, request.rx, request.tx, details))

    def _changed(self, change: S7ValuesChanged):
        self._callbacks.changed(change)
        if self._callbacks.written is not None:
            self._callbacks.written(WriteSpan((change.area, change.db), change.offset,
                                              change.offset + len(change.data), change.mask))

    async def bind(self):
        self._server = S7Server(self._plan, self._changed, self._record)
        await self._server.bind()

    async def shutdown(self):
        if self._server is not None:
            await self._server.shutdown()
            self._server = None

    def read_current(self, target: object) -> Value:
        if not isinstance(target, S7Target) or self._server is None:
            raise ValueError("需要正在监听的 S7 点位")
        return self._server.read_current(target)

    def write_current(self, target: object, value: Value):
        if not isinstance(target, S7Target) or self._server is None:
            raise ValueError("需要正在监听的 S7 点位")
        self._server.set_current(target, value)
