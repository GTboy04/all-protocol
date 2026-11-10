import asyncio
from contextlib import suppress
import struct

from ...application.contracts import ClientError, ClientEvents, Command, ErrorKind, Point, Target, Value
from .model import DataType, S7Config, S7Target
from .wire import COTP_DATA, READ_VAR, REQUESTED_PDU, REQUESTED_TPDU_EXPONENT, SETUP, WRITE_VAR, read_tpkt, tpkt


def _confirm_transport(response: bytes) -> int:
    if (len(response) < 7 or response[0] != len(response) - 1 or response[1] != 0xD0
            or response[2:4] != b"\x00\x01" or response[6] != 0):
        raise ClientError(ErrorKind.RESPONSE, "无效的 COTP 连接确认")
    exponent = 7
    offset = 7
    while offset < len(response):
        if offset + 2 > len(response):
            raise ClientError(ErrorKind.RESPONSE, "COTP 参数头不完整")
        code, size = response[offset:offset + 2]
        offset += 2
        if offset + size > len(response):
            raise ClientError(ErrorKind.RESPONSE, "COTP 参数长度无效")
        if code == 0xC0:
            if size != 1 or not 7 <= response[offset] <= REQUESTED_TPDU_EXPONENT:
                raise ClientError(ErrorKind.RESPONSE, "COTP 协商的 TPDU 大小无效")
            exponent = response[offset]
        offset += size
    return 1 << exponent


def _address(target: S7Target, *, bit_access: bool) -> bytes:
    if bit_access:
        word_length, count, bit = 0x01, 1, target.bit
    else:
        word_length, count, bit = 0x02, target.byte_count, 0
    bit_address = target.offset * 8 + bit
    return (struct.pack(">BBBBHHB", 0x12, 0x0A, 0x10, word_length, count, target.db, target.area)
            + bit_address.to_bytes(3, "big"))


class _S7StreamProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader, disconnected):
        super().__init__(reader)
        self._disconnected = disconnected

    def eof_received(self):
        super().eof_received()
        return False

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self._disconnected()


class S7Client:
    def __init__(self, config: S7Config, events: ClientEvents):
        self._config = config
        self._events = events
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport_closed = False
        self._request_active = False
        self._reference = 0
        self._tpdu_size = 1 << REQUESTED_TPDU_EXPONENT
        self._pdu_size = REQUESTED_PDU

    async def connect(self):
        self._config.validate()
        self._transport_closed = False
        try:
            async with asyncio.timeout(self._config.timeout_ms / 1000):
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                protocol = _S7StreamProtocol(reader, self._transport_lost)
                transport, _ = await loop.create_connection(lambda: protocol, self._config.host, self._config.port)
                self._reader = reader
                self._writer = asyncio.StreamWriter(transport, protocol, reader, loop)
                # Class 0 connection request, source reference 1, local TSAP 0x0100.
                request = bytes.fromhex("11 E0 00 00 00 01 00 C1 02 01 00 C2 02")
                request += struct.pack(">HBBB", self._config.remote_tsap, 0xC0, 1, REQUESTED_TPDU_EXPONENT)
                await self._send_frame(request)
                self._tpdu_size = _confirm_transport(await self._receive_frame())
                setup = struct.pack(">BBHHH", SETUP, 0, 1, 1, REQUESTED_PDU)
                params, data = await self._exchange(setup)
                if len(params) != 8 or params[:2] != setup[:2] or data:
                    raise ClientError(ErrorKind.RESPONSE, "无效的 S7 通信协商响应")
                caller, callee, pdu_size = struct.unpack(">HHH", params[2:])
                if not caller or not callee or not 24 <= pdu_size <= REQUESTED_PDU:
                    raise ClientError(ErrorKind.RESPONSE, "S7 通信协商参数超出范围")
                self._pdu_size = pdu_size
                if self._transport_closed:
                    raise ClientError(ErrorKind.CONNECT, "S7 对端已关闭 TCP 连接")
        except TimeoutError as error:
            raise ClientError(ErrorKind.TIMEOUT, "S7 连接握手超时") from error
        except (OSError, asyncio.IncompleteReadError) as error:
            raise ClientError(ErrorKind.CONNECT, f"S7 连接握手失败（{error}）") from error

    async def close(self):
        writer = self._writer
        self._reader = self._writer = None
        if writer is not None:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)

    def function(self, command: Command) -> str:
        return "Read Var (0x04)" if command.values is None else "Write Var (0x05)"

    async def read(self, target: Target) -> tuple[Point, ...]:
        target = self._target(target)
        target.validate()
        self._check_capacity(24, 18 + target.byte_count)
        params = bytes((READ_VAR, 1)) + _address(target, bit_access=False)
        payload = await self._request(params, read_size=target.byte_count)
        return target.decode(payload)

    async def write(self, target: Target, values: tuple[Value, ...]):
        target = self._target(target)
        payload = target.encode(values)
        self._check_capacity(28 + len(payload), 15)
        bit_access = target.data_type == DataType.BOOL
        params = bytes((WRITE_VAR, 1)) + _address(target, bit_access=bit_access)
        data = struct.pack(">BBH", 0, 0x03 if bit_access else 0x04, 1 if bit_access else len(payload) * 8)
        await self._request(params, data + payload)

    @staticmethod
    def _target(target: Target) -> S7Target:
        if not isinstance(target, S7Target):
            raise ClientError(ErrorKind.INPUT, "无效的 S7 点位")
        return target

    def _check_capacity(self, send_size: int, receive_size: int):
        if send_size > min(self._pdu_size, self._tpdu_size - 3) or receive_size > self._pdu_size:
            raise ClientError(ErrorKind.INPUT, "当前连接协商的报文容量较小，请减少点位数量")

    async def _request(self, params: bytes, data: bytes = b"", *, read_size: int = 0) -> bytes:
        self._request_active = True
        try:
            async with asyncio.timeout(self._config.timeout_ms / 1000):
                reply, payload = await self._exchange(params, data)
            if reply != params[:2]:
                raise ClientError(ErrorKind.RESPONSE, "S7 应答的功能码或项目数不匹配")
            if read_size:
                if len(payload) == 4 and payload[0] != 0xFF and payload[1:] == b"\x00\x00\x00":
                    raise ClientError(ErrorKind.PROTOCOL, "设备拒绝读取", payload[0])
                if payload[:4] != struct.pack(">BBH", 0xFF, 0x04, read_size * 8) or len(payload) != 4 + read_size:
                    raise ClientError(ErrorKind.RESPONSE, "S7 读取数据的类型或长度不匹配")
                return payload[4:]
            if len(payload) != 1:
                raise ClientError(ErrorKind.RESPONSE, "S7 写入应答缺少有效的项目状态")
            if payload[0] != 0xFF:
                raise ClientError(ErrorKind.PROTOCOL, "设备拒绝写入", payload[0], write_rejected=True)
            return b""
        except ClientError as error:
            if error.kind in (ErrorKind.RESPONSE, ErrorKind.DISCONNECTED):
                self._invalidate_connection()
            raise
        except TimeoutError as error:
            self._invalidate_connection()
            raise ClientError(ErrorKind.TIMEOUT, "等待 S7 应答超时") from error
        except (OSError, asyncio.IncompleteReadError) as error:
            self._invalidate_connection()
            raise ClientError(ErrorKind.DISCONNECTED, "S7 TCP 连接已中断") from error
        finally:
            self._request_active = False
            if self._transport_closed and self._writer is not None:
                self._events.disconnected()

    def _invalidate_connection(self):
        self._transport_lost()
        if self._writer is not None:
            self._writer.close()

    def _transport_lost(self):
        if self._transport_closed:
            return
        self._transport_closed = True
        # Let an active request interpret any final reply before the session shuts down.
        if not self._request_active and self._writer is not None:
            self._events.disconnected()

    async def _exchange(self, params: bytes, data: bytes = b"") -> tuple[bytes, bytes]:
        self._reference = self._reference % 65535 + 1
        header = struct.pack(">BBHHHH", 0x32, 1, 0, self._reference, len(params), len(data))
        await self._send_frame(COTP_DATA + b"\x80" + header + params + data)
        response = bytearray()
        while True:
            frame = await self._receive_frame()
            if len(frame) <= 3 or frame[:2] != COTP_DATA:
                raise ClientError(ErrorKind.RESPONSE, "无效的 COTP 数据帧")
            response.extend(frame[3:])
            if len(response) > self._pdu_size:
                raise ClientError(ErrorKind.RESPONSE, "S7 应答超过协商容量")
            if frame[2] & 0x80:
                break
        if len(response) < 12 or response[0] != 0x32 or response[1] not in (2, 3):
            raise ClientError(ErrorKind.RESPONSE, "无效的 S7 应答头")
        reference, param_size, data_size, error = struct.unpack(">HHHH", response[4:12])
        if reference != self._reference or len(response) != 12 + param_size + data_size:
            raise ClientError(ErrorKind.RESPONSE, "S7 应答引用号或报文长度不匹配")
        if error:
            raise ClientError(ErrorKind.PROTOCOL, "设备返回 S7 错误", error, write_rejected=params[0] == WRITE_VAR)
        if response[1] != 3:
            raise ClientError(ErrorKind.RESPONSE, "S7 应答未携带操作结果")
        return bytes(response[12:12 + param_size]), bytes(response[12 + param_size:])

    async def _send_frame(self, payload: bytes):
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ClientError(ErrorKind.DISCONNECTED, "S7 TCP 连接已关闭")
        frame = tpkt(payload)
        writer.write(frame)
        self._events.packet(True, frame)
        await writer.drain()

    async def _receive_frame(self) -> bytes:
        reader = self._reader
        if reader is None:
            raise ClientError(ErrorKind.DISCONNECTED, "S7 TCP 连接已关闭")
        return await read_tpkt(reader, self._tpdu_size, lambda frame: self._events.packet(False, frame))
