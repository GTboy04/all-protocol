import asyncio
from collections.abc import Callable
from datetime import datetime

from .contracts import Value
from .server_contracts import (
    LifecycleAction, ListenerState, ListenerUpdate, RequestMetadata, RuntimeChange,
    ServerAdapter, ServerCallbacks, ServerCapability, ServerEvent, ServerLifecycle, ServerRegistration,
)


class ServerSession:
    def __init__(self, emit: Callable[[ServerEvent], None], registration: ServerRegistration):
        self._emit = emit
        self._registration = registration
        self._adapter: ServerAdapter | None = None
        self._state = ListenerState.STOPPED
        self._endpoint = ""
        self._lifecycle = asyncio.Lock()

    def _publish(self, payload, time: datetime | None = None):
        self._emit(ServerEvent(self._registration.key, self._endpoint, time or datetime.now(), payload))

    def _update(self, state: ListenerState, error: str = ""):
        self._state = state
        self._publish(ListenerUpdate(state, error))

    def _request(self, time: datetime, metadata: RequestMetadata):
        self._publish(metadata, time)

    def _changed(self, details: object):
        self._publish(RuntimeChange(details))

    async def _shutdown(self):
        if self._adapter is not None:
            await self._adapter.shutdown()
            self._adapter = None

    async def start(self, plan: object):
        async with self._lifecycle:
            if self._state != ListenerState.STOPPED:
                return
            self._update(ListenerState.STARTING)
            try:
                await self._shutdown()
                self._endpoint = ""
                adapter = self._registration.create_adapter(plan, ServerCallbacks(self._request, self._changed))
                self._endpoint = adapter.endpoint
                adapter.validate_plan()
                self._adapter = adapter
                await adapter.bind()
            except (Exception, asyncio.CancelledError) as error:
                detail = " · ".join(filter(None, ("启动失败", self._endpoint, str(error) or type(error).__name__)))
                try:
                    await self._shutdown()
                except Exception as cleanup_error:
                    detail += f" · 清理失败：{cleanup_error}"
                finally:
                    self._publish(ServerLifecycle(LifecycleAction.START_FAILED, detail))
                    self._update(ListenerState.STOPPED, detail)
                    if self._adapter is None:
                        self._endpoint = ""
                if not isinstance(error, (ValueError, OSError)):
                    raise
                return
            self._publish(ServerLifecycle(LifecycleAction.STARTED, f"启动监听 · {self._endpoint}"))
            self._update(ListenerState.LISTENING)

    async def stop(self):
        async with self._lifecycle:
            if self._adapter is None:
                return
            previous = self._state
            self._update(ListenerState.STOPPING)
            try:
                await self._shutdown()
            except (Exception, asyncio.CancelledError) as error:
                self._update(previous, f"停止失败 · {error}")
                raise
            self._publish(ServerLifecycle(LifecycleAction.STOPPED, "已停止监听"))
            self._update(ListenerState.STOPPED)
            self._endpoint = ""

    def _active_adapter(self, capability: ServerCapability) -> ServerAdapter:
        if self._adapter is None or self._state != ListenerState.LISTENING:
            raise ValueError("服务未监听")
        if capability not in self._registration.capabilities:
            raise ValueError("协议不支持该运行值操作")
        return self._adapter

    async def read_current(self, target: object) -> Value:
        return self._active_adapter(ServerCapability.READ_CURRENT).read_current(target)

    async def write_current(self, target: object, value: Value):
        self._active_adapter(ServerCapability.WRITE_CURRENT).write_current(target, value)

    async def edit_current(self, target: object, value: Value):
        try:
            await self.write_current(target, value)
        except ValueError as error:
            self._update(self._state, str(error))
