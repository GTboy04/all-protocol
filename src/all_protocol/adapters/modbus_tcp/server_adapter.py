from ...application.contracts import Value
from ...application.server_contracts import RequestMetadata, ServerCallbacks, StationsChanged
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
        self._initial_units: frozenset[int] | None = None

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
        committed = self._callbacks.committed if any(station.extensions for station in self._plan.stations) else None
        self._server = TcpServer(self._plan, self._changed, self._record, self._callbacks.fault, committed)
        if self._initial_units is not None:
            self._server.context.active = set(self._initial_units)
        await self._server.bind()
        self._publish_stations()

    async def shutdown(self):
        if self._server is not None:
            await self._server.shutdown()
            self._server = None

    def read_current(self, target: object) -> Value:
        if not isinstance(target, ModbusServerTarget) or self._server is None:
            raise ValueError("需要正在监听的 Modbus TCP 点位")
        if not self.is_target_active(target):
            raise ValueError("从站未启动")
        return self._server.read_current(target.unit_id, target.area, target.offset)

    def write_current(self, target: object, value: Value):
        if not isinstance(target, ModbusServerTarget) or self._server is None:
            raise ValueError("需要正在监听的 Modbus TCP 点位")
        if not self.is_target_active(target):
            raise ValueError("从站未启动")
        self._server.set_current(target.unit_id, target.area, target.offset, value)

    def configure_stations(self, units: frozenset[int]):
        known = {station.unit_id for station in self._plan.stations}
        if not units <= known:
            raise ValueError("从站不存在")
        self._initial_units = units

    def write_batch(self, values):
        batches, seen = [], set()
        for target, value in values:
            if not isinstance(target, ModbusServerTarget) or not self.is_target_active(target):
                raise ValueError("输出点位不存在或从站未启动")
            point = self._server._point(target.unit_id, target.area, target.offset)
            keys = {(target.unit_id, target.area, address) for address in point.addresses}
            if seen & keys:
                raise ValueError("批次输出地址重叠")
            seen.update(keys)
            batches.append((target.unit_id, target.area, target.offset, point.encode(value)))
        self._server.context.commit(tuple(batches))

    def _publish_stations(self):
        self._callbacks.changed(StationsChanged(frozenset(self._server.context.active)))

    def set_station_active(self, unit_id: int, active: bool):
        if self._server is None or unit_id not in self._server.context.stations:
            raise ValueError("从站不存在")
        if active:
            self._server.context.active.add(unit_id)
        else:
            self._server.context.active.discard(unit_id)
        self._publish_stations()

    def is_target_active(self, target):
        return self._server is not None and target.unit_id in self._server.context.active
