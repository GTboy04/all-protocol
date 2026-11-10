import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import struct
from time import monotonic

from ...application.contracts import ClientError, ErrorKind, Value
from ...application.faults import FaultAction, FaultOperation
from .model import Area
from .server_model import S7Memory, S7ServerPlan, S7ValuesChanged
from .wire import COTP_DATA, READ_VAR, REQUESTED_PDU, REQUESTED_TPDU_EXPONENT, SETUP, WRITE_VAR, read_tpkt, tpkt


SUCCESS = 0xFF
OUT_OF_RANGE = 0x05
INVALID_TYPE = 0x06
INVALID_VALUE = 0x07
UNAVAILABLE = 0x0A
WORD_WIDTHS = {0x01: 1, 0x02: 1, 0x03: 1, 0x04: 2, 0x05: 2, 0x06: 4, 0x07: 4, 0x08: 4}


@dataclass(frozen=True, slots=True)
class S7ServerRequest:
    time: datetime
    peer: str
    function: int | None
    target: str
    result: str
    rx: bytes = b""
    tx: bytes = b""
    elapsed_ms: int | None = None
    error: ErrorKind | None = None


@dataclass(frozen=True, slots=True)
class _Item:
    word_length: int
    count: int
    db: int
    area: int
    address: int

    @classmethod
    def parse(cls, data: bytes):
        if data[:3] != b"\x12\x0A\x10":
            raise ValueError("无效的 S7ANY 地址格式")
        word_length, count, db, area = struct.unpack(">BHHB", data[3:9])
        return cls(word_length, count, db, area, int.from_bytes(data[9:], "big"))

    @property
    def size(self):
        return self.count * WORD_WIDTHS.get(self.word_length, 0)

    @property
    def offset(self):
        return self.address // 8

    @property
    def bit(self):
        return self.address % 8

    @property
    def bit_access(self):
        return self.word_length == 0x01

    @property
    def label(self):
        if self.area not in Area:
            return f"区域 0x{self.area:02X}"
        area = Area(self.area)
        prefix = f"DB{self.db}" if area == Area.DB else area.name
        if self.bit_access:
            return f"{prefix} {self.offset}.{self.bit}"
        return f"{prefix} {self.offset} +{self.size} B"

    def status(self):
        if self.area not in Area or (self.db != 0) != (self.area == Area.DB):
            return UNAVAILABLE
        if self.size == 0 or (self.bit_access and self.count != 1):
            return INVALID_TYPE
        if not self.bit_access and self.bit:
            return OUT_OF_RANGE
        return SUCCESS


def _connection_confirm(request: bytes) -> tuple[bytes, int]:
    if (len(request) < 7 or request[0] != len(request) - 1
            or request[1:4] != b"\xE0\x00\x00" or request[6] != 0):
        raise ValueError("需要 COTP Class 0 连接请求")
    exponent = 7
    tsaps = bytearray()
    offset = 7
    while offset < len(request):
        if offset + 2 > len(request):
            raise ValueError("COTP 参数头不完整")
        code, size = request[offset:offset + 2]
        end = offset + 2 + size
        if end > len(request):
            raise ValueError("COTP 参数长度无效")
        if code == 0xC0:
            value = request[offset + 2:end]
            if size != 1 or not 7 <= value[0] <= 13:
                raise ValueError("无效的 TPDU 大小")
            exponent = min(value[0], REQUESTED_TPDU_EXPONENT)
        elif code in (0xC1, 0xC2) and size == 2:
            tsaps.extend(request[offset:end])
        offset = end
    params = bytes((0xC0, 1, exponent)) + tsaps
    reply = bytes((6 + len(params), 0xD0)) + request[4:6] + b"\x00\x01\x00" + params
    return reply, 1 << exponent


def _ack(reference: int, params: bytes = b"", data: bytes = b"", *, error: int = 0) -> bytes:
    kind = 2 if error else 3
    return struct.pack(">BBHHHHH", 0x32, kind, 0, reference, len(params), len(data), error) + params + data


def _write_items(data: bytes, count: int) -> list[tuple[int, int, bytes]]:
    items = []
    offset = 0
    for index in range(count):
        if offset + 4 > len(data):
            raise ValueError("Write Var 数据头不完整")
        reserved, transport, length = struct.unpack_from(">BBH", data, offset)
        if reserved or transport not in (0x03, 0x04, 0x05, 0x07, 0x09):
            raise ValueError("Write Var 数据类型无效")
        size = (length + 7) // 8 if transport in (0x04, 0x05) else length
        start = offset + 4
        end = start + size
        if end > len(data):
            raise ValueError("Write Var 数据长度无效")
        items.append((transport, length, data[start:end]))
        offset = end + (size % 2 if index + 1 < count else 0)
    if offset != len(data):
        raise ValueError("Write Var 数据项数量或长度不匹配")
    return items


class S7Server:
    def __init__(self, plan: S7ServerPlan, changed, record, fault=None):
        self._plan = plan
        self._points = {point.target: point for point in plan.points}
        self._memory = S7Memory(plan.points)
        self._changed = changed
        self._record = record
        self._fault = fault
        self._listener: asyncio.Server | None = None
        self._clients: dict[asyncio.Task, asyncio.StreamWriter] = {}
        self._stopping = False

    async def bind(self):
        self._listener = await asyncio.start_server(
            self._accept, self._plan.host, self._plan.port, reuse_address=False,
        )

    async def shutdown(self):
        self._stopping = True
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        clients = tuple(self._clients.items())
        for task, writer in clients:
            writer.transport.abort()
            task.cancel()
        if clients:
            await asyncio.gather(*(task for task, _ in clients), return_exceptions=True)
        if listener is not None:
            await listener.wait_closed()

    def _point(self, target):
        point = self._points.get(target)
        if point is None:
            raise ValueError("点位不存在")
        return point

    def read_current(self, target) -> Value:
        return self._memory.current(self._point(target))

    def set_current(self, target, value: Value):
        self._changed(self._memory.set_point(self._point(target), value))

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self._stopping:
            writer.transport.abort()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._clients[task] = writer
        task.add_done_callback(self._clients.pop)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        host, port = writer.get_extra_info("peername")[:2]
        peer = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        rx = bytearray()

        def record(function, target, result, response=b"", *, started=None, error=None):
            elapsed = None if started is None else round((monotonic() - started) * 1000)
            self._record(S7ServerRequest(datetime.now(), peer, function, target, result, bytes(rx),
                                         response, elapsed, error))
            rx.clear()

        async def send(response: bytes, function: int | None, target: str, result: str, **metadata):
            writer.write(response)
            record(function, target, result, response, **metadata)
            await writer.drain()

        try:
            async with asyncio.timeout(5):
                request = await read_tpkt(reader, 1 << REQUESTED_TPDU_EXPONENT, rx.extend)
                confirm, tpdu_size = _connection_confirm(request)
                await send(tpkt(confirm), None, "COTP", "连接确认")
                setup = await self._read_job(reader, tpdu_size, REQUESTED_PDU, rx)
                reference, params, data = self._job(setup)
                if len(params) != 8 or params[:2] != b"\xF0\x00" or data:
                    raise ValueError("需要先完成 S7 通信协商")
                caller, callee, requested = struct.unpack(">HHH", params[2:])
                if not caller or not callee or requested < 24:
                    raise ValueError("S7 协商参数超出范围")
                pdu_size = min(REQUESTED_PDU, requested)
                reply = _ack(reference, struct.pack(">BBHHH", SETUP, 0, 1, 1, pdu_size))
                await send(self._frames(reply, tpdu_size), SETUP, "通信协商", f"PDU {pdu_size} B")
            while True:
                request = await self._read_job(reader, tpdu_size, pdu_size, rx)
                started = monotonic()
                reference, params, data = self._job(request)
                function = params[0]
                hit = None
                if function in (READ_VAR, WRITE_VAR) and self._fault is not None:
                    operation = FaultOperation.READ if function == READ_VAR else FaultOperation.WRITE
                    hit = self._fault(operation)
                action = hit.rule.action if hit else None
                if action == FaultAction.DISCONNECT:
                    record(function, "", "故障注入 · 主动断开", started=started, error=ErrorKind.DISCONNECTED)
                    return
                if action == FaultAction.EXCEPTION:
                    response, target, result = _ack(reference, error=0x8104), "", "异常 0x8104"
                else:
                    response, target, result = self._respond(reference, params, data, pdu_size)
                error = ErrorKind.PROTOCOL if result != "成功" else None
                if action == FaultAction.DELAY:
                    try:
                        await hit.wait()
                    except asyncio.CancelledError:
                        record(function, target, "故障注入 · 延迟应答中止",
                               started=started, error=ErrorKind.DISCONNECTED)
                        raise
                    if reader.at_eof() or writer.is_closing():
                        record(function, target, "故障注入 · 延迟期间连接中断",
                               started=started, error=ErrorKind.DISCONNECTED)
                        return
                if hit is not None:
                    result = f"故障注入 · {action} · {result}"
                await send(self._frames(response, tpdu_size), function, target, result, started=started, error=error)
        except (ClientError, ValueError, TimeoutError, asyncio.IncompleteReadError, OSError) as error:
            if rx:
                record(None, "", str(error), error=ErrorKind.RESPONSE)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    async def _read_job(reader, tpdu_size, pdu_size, rx) -> bytes:
        job = bytearray()
        while True:
            body = await read_tpkt(reader, tpdu_size, rx.extend)
            if len(body) < 4 or body[:2] != COTP_DATA:
                raise ValueError("无效的 COTP 数据帧")
            job.extend(body[3:])
            if len(job) > pdu_size:
                raise ValueError("S7 请求超过协商容量")
            if body[2] & 0x80:
                return bytes(job)

    @staticmethod
    def _frames(pdu: bytes, tpdu_size: int) -> bytes:
        size = tpdu_size - 3
        return b"".join(
            tpkt(COTP_DATA + bytes((0x80 if start + size >= len(pdu) else 0,)) + pdu[start:start + size])
            for start in range(0, len(pdu), size)
        )

    @staticmethod
    def _job(pdu: bytes) -> tuple[int, bytes, bytes]:
        if len(pdu) < 11 or pdu[:2] != b"\x32\x01":
            raise ValueError("无效的 S7 Job 报文头")
        reference, param_size, data_size = struct.unpack(">HHH", pdu[4:10])
        if not param_size or 10 + param_size + data_size != len(pdu):
            raise ValueError("S7 Job 长度不匹配")
        return reference, pdu[10:10 + param_size], pdu[10 + param_size:]

    def _respond(self, reference: int, params: bytes, data: bytes, pdu_size: int):
        function = params[0]
        if function not in (READ_VAR, WRITE_VAR):
            return _ack(reference, error=0x8104), "", "功能不支持"
        target = ""
        try:
            if len(params) < 2 or not params[1] or len(params) != 2 + 12 * params[1]:
                raise ValueError("S7 请求项目数量或参数长度无效")
            items = [_Item.parse(params[start:start + 12]) for start in range(2, len(params), 12)]
            target = "，".join(item.label for item in items)
            if function == READ_VAR:
                if data:
                    raise ValueError("Read Var 包含多余数据")
                sizes = [item.size if item.status() == SUCCESS else 0 for item in items]
                if 14 + 4 * len(items) + sum(sizes) + sum(size % 2 for size in sizes[:-1]) > pdu_size:
                    raise ValueError("读取数据超过协商的 PDU 容量")
                results = [self._read_item(item) for item in items]
                payload = b"".join(
                    result + (b"\x00" if len(result) % 2 and index + 1 < len(results) else b"")
                    for index, result in enumerate(results)
                )
                statuses = [result[0] for result in results]
            else:
                values = _write_items(data, len(items))
                statuses = [self._write_item(item, value) for item, value in zip(items, values, strict=True)]
                payload = bytes(statuses)
        except ValueError as error:
            return _ack(reference, error=0x8104), target, str(error)
        rejected = [f"0x{status:02X}" for status in statuses if status != SUCCESS]
        result = "成功" if not rejected else f"{len(statuses) - len(rejected)}/{len(statuses)} 成功 · {'、'.join(rejected)}"
        return _ack(reference, params[:2], payload), target, result

    def _read_item(self, item: _Item) -> bytes:
        status = item.status()
        if status != SUCCESS:
            return bytes((status, 0, 0, 0))
        try:
            value = self._memory.read(Area(item.area), item.db, item.offset, item.size)
        except KeyError:
            return bytes((OUT_OF_RANGE, 0, 0, 0))
        if item.bit_access:
            return bytes((SUCCESS, 0x03, 0, 1, (value[0] >> item.bit) & 1))
        return struct.pack(">BBH", SUCCESS, 0x04, len(value) * 8) + value

    def _write_item(self, item: _Item, value: tuple[int, int, bytes]) -> int:
        status = item.status()
        if status != SUCCESS:
            return status
        if item.area == Area.I:
            return UNAVAILABLE
        transport, length, data = value
        if item.bit_access:
            if (transport, length) != (0x03, 1) or data not in (b"\x00", b"\x01"):
                return INVALID_VALUE
        elif (transport == 0x03 or len(data) != item.size
              or (transport in (0x04, 0x05) and length != item.size * 8)):
            return INVALID_VALUE
        area = Area(item.area)
        try:
            previous = self._memory.read(area, item.db, item.offset, item.size)
        except KeyError:
            return OUT_OF_RANGE
        mask = 0xFF
        if item.bit_access:
            mask = 1 << item.bit
            data = bytes(((previous[0] & ~mask) | (data[0] << item.bit),))
        change = S7ValuesChanged(area, item.db, item.offset, data, mask)
        self._memory.apply(change)
        self._changed(change)
        return SUCCESS
