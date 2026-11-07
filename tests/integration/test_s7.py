import asyncio
import struct

import pytest
from snap7.server import Server
from snap7.type import SrvArea

from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import MAX_OFFSET, Area, DataType, S7Config, S7Target
from all_protocol.application.contracts import (
    ClientError, Command, ConnectionState, ErrorKind, JournalEntry, ProtocolRegistration, SessionState,
)
from all_protocol.application.session import Session


@pytest.fixture
def plc_port():
    server = Server()
    for area, index in ((SrvArea.DB, 1), (SrvArea.PE, 0), (SrvArea.PA, 0), (SrvArea.MK, 0)):
        server.register_area(area, index, bytearray(b"\xA5\x00" + bytes(254)))
    server.start_to("127.0.0.1", 0)
    try:
        yield server.server_socket.getsockname()[1]
    finally:
        server.stop()


def test_s7_session_interoperates_with_snap7(plc_port):
    async def run():
        events = []
        session = Session(events.append)
        config = S7Config("127.0.0.1", plc_port)
        protocol = ProtocolRegistration("s7", "Siemens S7", S7Client, True)
        await session.connect(protocol, config, config.endpoint)
        try:
            assert events[-1].connection == ConnectionState.ONLINE
            assert [sending for sending, _ in events[-2].packets] == [True, False, True, False]

            for area in Area:
                target = S7Target(area, int(area == Area.DB), 0, DataType.BOOL, bit=7)
                await session.execute(Command(target))
                assert events[-2].points[0].value is True
                assert events[-2].points[0].hex_value == "A5"
            for data_type, values, raw in (
                (DataType.BYTE, (1, 255), ("01", "FF")),
                (DataType.INT, (-123, 456), ("FF 85", "01 C8")),
                (DataType.WORD, (0xA1B2, 0xC3D4), ("A1 B2", "C3 D4")),
                (DataType.DINT, (-2, 3), ("FF FF FF FE", "00 00 00 03")),
                (DataType.DWORD, (0x89ABCDEF, 0x01234567), ("89 AB CD EF", "01 23 45 67")),
                (DataType.REAL, (-12.5, 4.25), ("C1 48 00 00", "40 88 00 00")),
            ):
                target = S7Target(Area.DB, 1, 2, data_type, 2)
                await session.execute(Command(target, values))
                assert events[-2].outcome == "设备已应答"
                await session.execute(Command(target))
                points = events[-2].points
                assert tuple(point.value for point in points) == values
                assert tuple(point.hex_value for point in points) == raw
                assert points[1].address == target.address(1)
                assert [sending for sending, _ in events[-2].entry.packets] == [True, False]

            await session.execute(Command(S7Target(Area.DB, 2, 0, DataType.BYTE), (42,)))
            assert events[-2].outcome == ErrorKind.PROTOCOL
            assert "0x8404" in events[-2].detail
            assert events[-1].connection == ConnectionState.ONLINE
            await session.execute(Command(S7Target(Area.DB, 2, 0, DataType.BYTE)))
            assert events[-2].entry.error == ErrorKind.PROTOCOL
            assert "0x0A" in events[-2].entry.detail
            await session.execute(Command(S7Target(Area.I, 0, 0, DataType.BYTE), (42,)))
            assert events[-2].error == ErrorKind.INPUT and not events[-2].packets
        finally:
            await session.disconnect()

    asyncio.run(run())


async def _frame(reader):
    header = await reader.readexactly(4)
    assert header[:2] == b"\x03\x00"
    return await reader.readexactly(int.from_bytes(header[2:], "big") - 4)


def _tpkt(cotp):
    return struct.pack(">BBH", 3, 0, len(cotp) + 4) + cotp


def _response(request, params, data=b""):
    return b"\x32\x03\x00\x00" + request[7:9] + struct.pack(">HHH", len(params), len(data), 0) + params + data


@pytest.mark.parametrize("fault", [None, "timeout", "wrong_reference", "eof"])
def test_s7_native_bit_write_and_write_acknowledgement(fault):
    async def run():
        handled = asyncio.get_running_loop().create_future()

        async def peer(reader, writer):
            try:
                request = await _frame(reader)
                assert request == bytes.fromhex("11 E0 00 00 00 01 00 C1 02 01 00 C2 02 01 64 C0 01 0A")
                writer.write(_tpkt(bytes.fromhex("09 D0 00 01 00 01 00 C0 01 0A")))
                setup = await _frame(reader)
                setup_ack = _response(setup, bytes.fromhex("F0 00 00 01 00 01 01 E0"))
                writer.write(_tpkt(b"\x02\xF0\x00" + setup_ack[:8]))
                writer.write(_tpkt(b"\x02\xF0\x80" + setup_ack[8:]))
                write = await _frame(reader)
                assert write[13:] == bytes.fromhex("05 01 12 0A 10 01 00 01 00 01 84 00 00 17 00 03 00 01 01")
                if fault in (None, "wrong_reference"):
                    reply = _response(write, b"\x05\x01", b"\xFF")
                    if fault:
                        reply = reply[:4] + b"\x00\x07" + reply[6:]
                    writer.write(_tpkt(b"\x02\xF0\x80" + reply))
                if fault != "eof":
                    assert await reader.read() == b""
            except Exception as error:
                handled.set_exception(error)
            else:
                handled.set_result(None)
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(peer, "127.0.0.1", 0) as server:
            events = []
            session = Session(events.append)
            config = S7Config("127.0.0.1", server.sockets[0].getsockname()[1], 3, 4, 200)
            await session.connect(ProtocolRegistration("s7", "S7", S7Client, True), config, config.endpoint)
            assert events[-1].connection == ConnectionState.ONLINE
            await session.execute(Command(S7Target(Area.DB, 1, 2, DataType.BOOL, bit=7), (True,)))
            entry = next(event for event in events if isinstance(event, JournalEntry) and event.operation == "写入")
            if fault is None:
                assert entry.outcome == "设备已应答"
                await session.disconnect()
            else:
                assert entry.outcome == "写入结果未知"
                assert entry.error == {"timeout": ErrorKind.TIMEOUT, "wrong_reference": ErrorKind.RESPONSE,
                                       "eof": ErrorKind.DISCONNECTED}[fault]
                await asyncio.wait_for(handled, 2)
                await session.disconnect()
                assert isinstance(events[-1], SessionState) and events[-1].connection == ConnectionState.OFFLINE
            assert sum(sending for sending, _ in entry.packets) == 1
            await asyncio.wait_for(handled, 2)

    asyncio.run(run())


def test_s7_target_checks_end_address_and_write_values():
    S7Target(Area.M, 0, MAX_OFFSET, DataType.BOOL, bit=7).validate()
    with pytest.raises(ClientError, match="字节地址"):
        S7Target(Area.M, 0, MAX_OFFSET, DataType.WORD).validate()
    with pytest.raises(ClientError, match="取值范围"):
        S7Target(Area.Q, 0, 0, DataType.REAL).encode((float("inf"),))
    with pytest.raises(ClientError, match="取值范围"):
        S7Target(Area.DB, 1, 0, DataType.INT).encode((32768,))
