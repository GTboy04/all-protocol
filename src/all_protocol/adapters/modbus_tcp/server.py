import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ExceptionResponse
from pymodbus.server import ModbusTcpServer
from pymodbus.server.base import ModbusBaseServer
from pymodbus.server.requesthandler import ServerRequestHandler

from ...application.contracts import Value
from .model import RULES, Area
from .server_model import ServerPlan


READ_AREAS = {rule.read_code: area for area, rule in RULES.items()}
WRITE_AREAS = {code: area for area, rule in RULES.items() for code in rule.write_codes or ()}
AREAS = READ_AREAS | WRITE_AREAS
SINGLE_WRITES = {rule.write_codes[0] for rule in RULES.values() if rule.write_codes}
MULTI_WRITES = WRITE_AREAS.keys() - SINGLE_WRITES


@dataclass(frozen=True, slots=True)
class ValuesChanged:
    unit_id: int
    area: Area
    offset: int
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ServerRequest:
    time: datetime
    peer: str
    unit_id: int | None
    function: int | None
    offset: int | None
    count: int | None
    result: str
    rx: bytes = b""
    tx: bytes = b""


class RuntimeValues:
    """Sparse zero-based datastore. Access stays on the server's asyncio loop."""

    def __init__(self, plan: ServerPlan, changed: Callable[[ValuesChanged], None]):
        self.stations = {
            station.unit_id: {
                (point.area, address): word
                for point in station.points
                for address, word in zip(point.addresses, point.encode(point.initial), strict=True)
            }
            for station in plan.stations
        }
        self.points = {
            (station.unit_id, point.area, point.offset): point
            for station in plan.stations for point in station.points
        }
        self.changed = changed

    async def async_getValues(self, device_id, func_code, address, count=1):
        station = self.stations[device_id]
        area = AREAS[func_code]
        try:
            return [station[area, index] for index in range(address, address + count)]
        except KeyError:
            return ExcCodes.ILLEGAL_ADDRESS

    async def async_setValues(self, device_id, func_code, address, values):
        return self.set_values(device_id, WRITE_AREAS[func_code], address, tuple(map(int, values)))

    def set_values(self, unit_id: int, area: Area, offset: int, values: tuple[int, ...]):
        station = self.stations.get(unit_id)
        if station is None:
            return ExcCodes.GATEWAY_NO_RESPONSE
        keys = [(area, index) for index in range(offset, offset + len(values))]
        if any(key not in station for key in keys):
            return ExcCodes.ILLEGAL_ADDRESS
        station.update(zip(keys, values, strict=True))
        self.changed(ValuesChanged(unit_id, area, offset, values))
        return None


class _Connection(ServerRequestHandler):
    def __init__(self, server):
        super().__init__(server, None, None, None)
        self.rejecting = False

    def connection_made(self, transport):
        if self.is_closing:
            transport.close()
            return
        super().connection_made(transport)

    def data_received(self, data: bytes):
        if self.rejecting:
            return
        # Trace complete ADUs. Transport chunks may contain partial or multiple requests.
        data = self.recv_buffer + data
        cursor = 0
        frames = []
        while len(data) - cursor >= 6:
            size = int.from_bytes(data[cursor + 4:cursor + 6], "big")
            if data[cursor + 2:cursor + 4] != b"\x00\x00" or not 2 <= size <= 254:
                self.rejecting = True
                break
            end = cursor + 6 + size
            if end > len(data):
                break
            frames.append(data[cursor:end])
            cursor = end
        self.recv_buffer = b"" if self.rejecting else data[cursor:]
        if frames or self.rejecting:
            task = self.loop.create_task(self._process(frames, self.rejecting))
            self.server.requests.add(task)
            task.add_done_callback(self.server.requests.discard)

    async def _process(self, frames: list[bytes], close_after: bool):
        if self.transport is None:
            return
        peer = self.transport.get_extra_info("peername")
        host, port = peer[:2]
        source = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        for rx in frames:
            transaction = int.from_bytes(rx[:2], "big")
            unit, function = rx[6:8]
            pdu = rx[7:]
            offset = int.from_bytes(pdu[1:3], "big") if function in AREAS and len(pdu) >= 3 else None
            count = (1 if function in SINGLE_WRITES else int.from_bytes(pdu[3:5], "big")
                     if function in AREAS and len(pdu) >= 5 else None)
            response = await self.server.respond(unit, pdu)
            response.dev_id = unit
            response.transaction_id = transaction
            tx = self.framer.buildFrame(response)
            self.send(tx)
            result = f"异常 0x{response.exception_code:02X}" if response.isError() else "成功"
            self.server.record(ServerRequest(datetime.now(), source, unit, function, offset, count, result, rx, tx))
        if close_after:
            self.close()


class TcpServer(ModbusTcpServer):
    def __init__(self, plan: ServerPlan, changed, record):
        super().__init__([], address=(plan.host, plan.port))
        # Use PyModbus PDU datastore operations with exact Unit IDs and sparse bit addresses.
        self.context = RuntimeValues(plan, changed)
        self.record = record
        self.requests: set[asyncio.Task] = set()

    def callback_new_connection(self):
        return _Connection(self)

    async def bind(self):
        self.is_listener = True
        host, port = self.comm_params.source_address
        # Report the socket error to the UI and keep the listening port exclusive on Windows.
        self.transport = await self.loop.create_server(self.handle_new_connection, host, port, reuse_address=False)

    async def shutdown(self):
        listener = self.transport
        for task in self.requests:
            task.cancel()
        await super().shutdown()
        if self.requests:
            await asyncio.gather(*self.requests, return_exceptions=True)
        if listener is not None:
            await listener.wait_closed()
        if ModbusBaseServer.active_server is self:
            ModbusBaseServer.active_server = None

    async def respond(self, unit: int, pdu: bytes):
        code = pdu[0]
        if code not in AREAS:
            return ExceptionResponse(code, ExcCodes.ILLEGAL_FUNCTION)
        if unit not in self.context.stations:
            return ExceptionResponse(code, ExcCodes.GATEWAY_NO_RESPONSE)
        if code in MULTI_WRITES:
            limit = RULES[WRITE_AREAS[code]].write_limit
            if len(pdu) < 6 or not 1 <= int.from_bytes(pdu[3:5], "big") <= limit:
                return ExceptionResponse(code, ExcCodes.ILLEGAL_VALUE)
        elif len(pdu) != 5:
            return ExceptionResponse(code, ExcCodes.ILLEGAL_VALUE)
        try:
            request = self.decoder.decode(pdu)
        except ModbusIOException:
            return ExceptionResponse(code, ExcCodes.ILLEGAL_VALUE)
        request.dev_id = unit
        try:
            return await request.datastore_update(self.context, unit)
        except Exception:
            logging.exception("Modbus TCP request failed")
            return ExceptionResponse(code, ExcCodes.DEVICE_FAILURE)

    def set_current(self, unit: int, area: Area, offset: int, value: Value):
        point = self.context.points.get((unit, area, offset))
        if point is None:
            raise ValueError("点位不存在")
        self.context.set_values(unit, area, offset, point.encode(value))
