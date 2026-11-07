import asyncio
import struct
from collections.abc import Callable

from ...application.contracts import ClientError, ErrorKind


SETUP = 0xF0
READ_VAR = 0x04
WRITE_VAR = 0x05
REQUESTED_PDU = 480
REQUESTED_TPDU_EXPONENT = 10
COTP_DATA = b"\x02\xF0"


def tpkt(payload: bytes) -> bytes:
    return struct.pack(">BBH", 3, 0, len(payload) + 4) + payload


async def read_tpkt(reader: asyncio.StreamReader, tpdu_size: int, trace: Callable[[bytes], None]) -> bytes:
    header = b""
    try:
        header = await reader.readexactly(4)
        version, reserved, size = struct.unpack(">BBH", header)
        if (version, reserved) != (3, 0) or not 7 <= size <= tpdu_size + 4:
            trace(header)
            raise ClientError(ErrorKind.RESPONSE, "无效的 TPKT 报文头")
        body = await reader.readexactly(size - 4)
    except asyncio.IncompleteReadError as error:
        if header or error.partial:
            trace(header + error.partial)
        raise
    trace(header + body)
    return body
