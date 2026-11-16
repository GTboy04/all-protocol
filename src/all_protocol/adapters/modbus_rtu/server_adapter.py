from ..modbus_tcp.server_adapter import ModbusServerAdapter
from .server_model import RtuServerPlan
from .server import RtuServer
from .ports import acquire, release


class RtuServerAdapter(ModbusServerAdapter):
    def __init__(self, plan, callbacks):
        if not isinstance(plan, RtuServerPlan):
            raise ValueError("需要 Modbus RTU 服务端方案")
        plan.validate()
        self._plan, self._callbacks, self._server = plan, callbacks, None

    @property
    def endpoint(self):
        return self._plan.endpoint

    async def bind(self):
        acquire(self.endpoint, self)
        try:
            self._server = RtuServer(self._plan, self._changed, self._record, self._callbacks.fault)
            await self._server.bind()
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self):
        try:
            await super().shutdown()
        finally:
            release(self.endpoint, self)
