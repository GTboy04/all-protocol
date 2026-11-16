import asyncio
from datetime import datetime
from pymodbus.framer.rtu import FramerRTU
from pymodbus.server import ModbusSerialServer
from pymodbus.server.base import ModbusBaseServer
from ..modbus_tcp.server import TcpServer, RuntimeValues, _Connection, ServerRequest
from ...application.contracts import ErrorKind


class RtuConnection(_Connection):
    def __init__(self, server):
        super().__init__(server)
        self._boundary = None
        self._request_lock = asyncio.Lock()

    def _peer(self):
        return self.server.plan.endpoint

    def data_received(self, data):
        if self._boundary is not None:
            self._boundary.cancel()
        self.recv_buffer += data
        # USB serial drivers batch bytes. Use the configured silent interval to
        # collect a frame; validate exact length and CRC before touching datastore.
        self._boundary = self.loop.call_later(self.server.plan.config.silent_interval, self._frame)

    def _frame(self):
        self._boundary = None
        frame, self.recv_buffer = self.recv_buffer, b""
        if not frame:
            return
        valid = 4 <= len(frame) <= 256 and FramerRTU.check_CRC(frame[:-2], int.from_bytes(frame[-2:], "big"))
        if not valid:
            self.server.record(ServerRequest(datetime.now(), self._peer(), None, None, None, None,
                                             "RTU CRC 错误或帧残缺", frame, error=ErrorKind.RESPONSE))
        elif frame[0] in self.server.context.stations:
            self._track(self._serial_process(frame))
        # Broadcast and foreign stations are silent on a shared bus.

    async def _serial_process(self, frame):
        async with self._request_lock:
            await self._process(frame, rtu=True)

    def close(self, reconnect=False):
        if self._boundary is not None:
            self._boundary.cancel()
            self._boundary = None
        self.recv_buffer = b""
        super().close(reconnect=reconnect)

    def connection_lost(self, exc):
        if self._boundary is not None:
            self._boundary.cancel()
            self._boundary = None
        if self.recv_buffer:
            self._frame()
        super().connection_lost(exc)


class RtuServer(ModbusSerialServer):
    respond = TcpServer.respond
    _point = TcpServer._point
    read_current = TcpServer.read_current
    set_current = TcpServer.set_current

    def __init__(self, plan, changed, record, fault=None):
        super().__init__([], port=plan.endpoint, baudrate=plan.baudrate, parity=plan.parity,
                         stopbits=plan.stopbits, bytesize=8, timeout=plan.timeout_ms / 1000, reconnect_delay=0)
        self.plan = plan
        self.context = RuntimeValues(plan, changed)
        self.record, self.fault = record, fault
        self.requests = set()

    def callback_new_connection(self):
        return RtuConnection(self)

    async def bind(self):
        if not await self.listen():
            raise OSError("无法打开串口，请检查端口、驱动及占用情况")
        # PyModbus schedules transport.setup and connection_made on consecutive turns.
        # Do not expose a bound server before both callbacks can safely be closed.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def shutdown(self):
        pending = tuple(self.requests)
        for task in pending:
            task.cancel()
        await super().shutdown()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if ModbusBaseServer.active_server is self:
            ModbusBaseServer.active_server = None
