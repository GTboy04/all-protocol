"""Owns multiple device sessions on one asyncio event loop."""

import asyncio
from dataclasses import dataclass, field, replace
from uuid import uuid4

from .contracts import (
    BatchResult, ConnectionState, DeviceEvent, DeviceRemoved, DeviceResult, JournalEntry,
    OperationRejected, ReadUpdate, SessionState, TableReadUpdate,
)
from .session import Session


@dataclass
class DeviceRuntime:
    id: str
    runtime_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    session: Session | None = None
    state: SessionState = field(default_factory=SessionState)
    config_key: object = None
    retiring: bool = False
    last_error: str = ""


class SessionManager:
    def __init__(self, emit, *, create_session=Session):
        self.emit, self.create_session = emit, create_session
        self.devices: dict[str, DeviceRuntime] = {}
        self._closing = False
        self._disconnecting = False
        self._batch_lock = asyncio.Lock()

    def for_device(self, device_id):
        return DeviceSession(self, device_id)

    def _forward(self, runtime, payload):
        if self.devices.get(runtime.id) is not runtime:
            return
        if isinstance(payload, SessionState):
            payload = replace(payload, device_id=runtime.id, session_id=runtime.session_id)
            runtime.state = payload
        elif isinstance(payload, JournalEntry):
            payload = replace(payload, device_id=runtime.id, session_id=runtime.session_id)
            if payload.operation == "连接":
                runtime.last_error = payload.detail if payload.error else ""
        elif isinstance(payload, ReadUpdate):
            payload = replace(payload, entry=replace(payload.entry, device_id=runtime.id, session_id=runtime.session_id))
        elif isinstance(payload, TableReadUpdate):
            update = payload.update
            payload = replace(payload, update=replace(update, entry=replace(update.entry, device_id=runtime.id,
                                                                            session_id=runtime.session_id)))
        generation = (payload.update.generation if isinstance(payload, TableReadUpdate)
                      else getattr(payload, "generation", runtime.state.generation))
        self.emit(DeviceEvent(runtime.id, runtime.session_id, generation, runtime.runtime_id, payload,
                              runtime.config_key[2] if runtime.config_key is not None else None))

    def _reject(self, device_id, detail):
        runtime = self.devices.get(device_id)
        if runtime:
            runtime.last_error = detail
            self._forward(runtime, OperationRejected("设备操作", detail, runtime.state.generation))
            self._forward(runtime, runtime.state)
        else:
            self.emit(DeviceEvent(device_id, "", 0, "", OperationRejected("设备操作", detail, 0)))
            self.emit(DeviceEvent(device_id, "", 0, "", SessionState(device_id=device_id or "")))
        return False

    async def connect(self, device_id, protocol, config, endpoint):
        if not device_id or self._closing or self._disconnecting:
            return self._reject(device_id, "正在清理连接，连接请求未执行")
        runtime = self.devices.get(device_id)
        if runtime is None:
            runtime = DeviceRuntime(device_id)
            self.devices[device_id] = runtime
            try:
                runtime.session = self.create_session(lambda event: self._forward(runtime, event))
            except Exception:
                self.devices.pop(device_id, None)
                raise
        key = protocol.key, endpoint, config
        if runtime.retiring:
            return self._reject(device_id, "设备正在移除，请求未执行")
        if runtime.state.connection != ConnectionState.OFFLINE:
            if runtime.state.connection == ConnectionState.ONLINE and runtime.config_key == key:
                self._forward(runtime, runtime.state)
                return True
            return self._reject(device_id, "请先断开该设备，再修改连接参数或重新连接")
        runtime.config_key = key
        runtime.session_id = str(uuid4())
        runtime.last_error = ""
        try:
            await runtime.session.connect(protocol, config, endpoint)
        except asyncio.CancelledError:
            runtime.last_error = "连接已取消"
            return False
        return runtime.state.connection == ConnectionState.ONLINE

    async def disconnect(self, device_id):
        runtime = self.devices.get(device_id)
        if runtime is not None:
            await runtime.session.disconnect()
        return True

    async def call(self, device_id, method, *args):
        if method not in {"execute", "execute_table", "start_table_polling", "stop_table_polling", "stop_polling", "stop_all_polling"}:
            raise ValueError("不支持的设备操作")
        runtime = self.devices.get(device_id)
        if runtime is None or runtime.retiring or self._closing or self._disconnecting:
            return self._reject(device_id, "设备不可用，请求未发送")
        return await getattr(runtime.session, method)(*args)

    async def connect_all(self, specifications):
        async def one(spec):
            device_id, protocol, config, endpoint = spec
            try:
                success = await self.connect(device_id, protocol, config, endpoint)
                runtime = self.devices.get(device_id)
                return DeviceResult(device_id, success, runtime.last_error if runtime else "设备不可用")
            except Exception as error:
                self._reject(device_id, str(error))
                return DeviceResult(device_id, False, str(error))
        result = BatchResult("连接", tuple(await asyncio.gather(*(one(spec) for spec in specifications))))
        self.emit(DeviceEvent("", "", 0, "", result))
        return result

    async def disconnect_all(self):
        async with self._batch_lock:
            self._disconnecting = True
            try:
                ids = tuple(self.devices)
                outcomes = await asyncio.gather(*(self.disconnect(device_id) for device_id in ids), return_exceptions=True)
                result = BatchResult("断开", tuple(DeviceResult(device_id, not isinstance(outcome, BaseException),
                                                               str(outcome) if isinstance(outcome, BaseException) else "")
                                                    for device_id, outcome in zip(ids, outcomes, strict=True)))
                self.emit(DeviceEvent("", "", 0, "", result))
                return result
            finally:
                self._disconnecting = False

    async def remove(self, device_id):
        runtime = self.devices.get(device_id)
        if runtime is None:
            self.emit(DeviceEvent(device_id, "", 0, "", DeviceRemoved()))
            return True
        if runtime.retiring:
            return False
        runtime.retiring = True
        try:
            await runtime.session.disconnect()
        except BaseException:
            runtime.retiring = False
            raise
        self._forward(runtime, DeviceRemoved())
        self.devices.pop(device_id, None)
        return True

    async def shutdown(self):
        self._closing = True
        await self.disconnect_all()
        self.devices.clear()


class DeviceSession:
    """A device-bound command handle; creating it touches no event-loop state."""

    def __init__(self, manager, device_id):
        self.manager, self.device_id = manager, device_id

    async def connect(self, protocol, config, endpoint):
        return await self.manager.connect(self.device_id, protocol, config, endpoint)

    async def disconnect(self):
        return await self.manager.disconnect(self.device_id)

    async def execute(self, command, interval_ms=None):
        return await self.manager.call(self.device_id, "execute", command, interval_ms)

    async def execute_table(self, table_id, commands):
        return await self.manager.call(self.device_id, "execute_table", table_id, commands)

    async def start_table_polling(self, table_id, commands, interval_ms):
        return await self.manager.call(self.device_id, "start_table_polling", table_id, commands, interval_ms)

    async def stop_table_polling(self, table_id):
        return await self.manager.call(self.device_id, "stop_table_polling", table_id)

    async def stop_polling(self):
        return await self.manager.call(self.device_id, "stop_polling")

    async def stop_all_polling(self):
        return await self.manager.call(self.device_id, "stop_all_polling")
