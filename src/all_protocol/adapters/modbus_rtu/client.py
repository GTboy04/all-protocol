import asyncio
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException
from ..modbus_tcp.common import ModbusClient
from ...application.contracts import ClientError, ErrorKind
from .model import ModbusRtuConfig, ModbusRtuTarget
from .ports import acquire, release
from .framer import StrictRtuFramer


class ModbusRtuClient(ModbusClient):
    def __init__(self, config, events):
        if not isinstance(config, ModbusRtuConfig):
            raise ClientError(ErrorKind.INPUT, "需要 Modbus RTU 串口配置")
        config.validate()
        self._config, self._events = config, events
        self._received = False
        self._receive_buffer = b""
        self._blocked = set()
        self._lock = asyncio.Lock()
        self._client = AsyncModbusSerialClient(
            config.endpoint, baudrate=config.baudrate, parity=config.parity, stopbits=config.stopbits,
            bytesize=8, timeout=config.timeout_ms / 1000, retries=0, reconnect_delay=0,
            trace_packet=self._trace_packet, trace_connect=self._trace_connect)
        self._client.ctx.framer = StrictRtuFramer(self._client.ctx.framer.decoder)

    def _trace_packet(self, sending, data):
        if not sending:
            self._received = True
            chunk = data[len(self._receive_buffer):] if data.startswith(self._receive_buffer) else data
            self._receive_buffer = data
            if chunk:
                self._events.packet(False, chunk)
        else:
            self._events.packet(True, data)
        return data

    def _flush_partial_response(self):
        pass  # RTU logs the actual transport chunks, including invalid or partial frames.

    def _trace_connect(self, connected):
        if not connected:
            self._events.disconnected()

    def _matches_response(self, response, device_id):
        return response.dev_id == device_id

    async def connect(self):
        try:
            acquire(self._config.endpoint, self)
            if not await self._client.connect() or not self._client.connected:
                raise OSError("无法打开串口，请检查端口、驱动及占用情况")
            self._blocked.clear()
        except BaseException as error:
            self._client.close()
            release(self._config.endpoint, self)
            if isinstance(error, (OSError, ModbusException)):
                raise ClientError(ErrorKind.CONNECT, str(error)) from error
            raise

    async def close(self):
        try:
            self._client.close()
        finally:
            release(self._config.endpoint, self)

    def _target(self, target):
        if not isinstance(target, ModbusRtuTarget):
            raise ClientError(ErrorKind.INPUT, "需要 Modbus RTU 点位")
        target.validate()

    async def read(self, target):
        self._target(target)
        try:
            return await super().read(target)
        except ClientError as error:
            if error.kind == ErrorKind.RESPONSE:
                self._blocked.add(target.unit_id)
            raise

    async def write(self, target, values):
        self._target(target)
        try:
            await super().write(target, values)
        except ClientError as error:
            if error.kind == ErrorKind.RESPONSE:
                self._blocked.add(target.unit_id)
            raise

    async def _exchange(self, operation, *args, code, device_id, **kwargs):
        async with self._lock:
            if device_id in self._blocked:
                raise ClientError(ErrorKind.INPUT, "该站号上次请求无有效应答，已暂停；检查链路后重新连接")
            if not self._client.connected:
                raise ClientError(ErrorKind.DISCONNECTED, "串口已关闭")
            # No transaction IDs: never reuse an ambiguous station until explicit reconnect.
            await asyncio.sleep(self._config.silent_interval)
            self._receive_buffer = b""
            self._client.ctx.recv_buffer = b""
            try:
                transport = self._client.ctx.transport
                if transport is not None:
                    transport.sync_serial.reset_input_buffer()
            except OSError as error:
                self._client.close()
                self._events.disconnected()
                raise ClientError(ErrorKind.DISCONNECTED, str(error)) from error
            try:
                return await super()._exchange(operation, *args, code=code, device_id=device_id, **kwargs)
            except ClientError as error:
                if error.kind in (ErrorKind.TIMEOUT, ErrorKind.RESPONSE, ErrorKind.DISCONNECTED):
                    self._blocked.add(device_id)
                raise
            except asyncio.CancelledError:
                self._blocked.add(device_id)
                raise
