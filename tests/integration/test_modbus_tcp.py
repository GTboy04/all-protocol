import asyncio
import struct
from contextlib import asynccontextmanager

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.application.contracts import (
    ClientError, Command, ConnectionState, ErrorKind, JournalEntry, ProtocolRegistration,
    ReadUpdate, SessionState,
)
from all_protocol.application.session import Session


MODBUS = ProtocolRegistration("modbus-tcp", "Modbus TCP", ModbusTcpClient, True)


@asynccontextmanager
async def tcp_peer(reply, timeout_ms=120):
    """Independent Modbus TCP peer with controllable faults."""
    received = []
    clients = {}

    async def handle(reader, writer):
        clients[writer] = asyncio.current_task()
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                assert protocol == 0
                request = await reader.readexactly(length - 1)
                received.append((unit, request))
                response = reply(unit, request)
                if response is False:
                    return
                if response is not None:
                    writer.write(struct.pack(">HHHB", transaction, 0, len(response) + 1, unit) + response)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            clients.pop(writer, None)

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            yield ModbusTcpConfig("127.0.0.1", port, timeout_ms), received
        finally:
            pending = list(clients.values())
            for writer in list(clients):
                writer.close()
            await asyncio.gather(*pending)


def entries(events):
    return [event for event in events if isinstance(event, JournalEntry)]


def readings(events):
    return [event for event in events if isinstance(event, ReadUpdate)]


def last_state(events):
    return next(event for event in reversed(events) if isinstance(event, SessionState))


def reply_from_memory(memory):
    def reply(unit, request):
        function, address, amount = struct.unpack(">BHH", request[:5])
        if function in (3, 4):
            registers = [memory.get((unit, function, address + index), unit * 100 + index) for index in range(amount)]
            return bytes((function, amount * 2)) + struct.pack(f">{amount}H", *registers)
        if function in (1, 2):
            packed = bytearray((amount + 7) // 8)
            for index in range(amount):
                packed[index // 8] |= memory.get((unit, function, address + index), 1) << (index % 8)
            return bytes((function, len(packed))) + packed
        if function == 6:
            memory[unit, 3, address] = amount
        elif function == 5:
            memory[unit, 1, address] = int(amount == 0xFF00)
        elif function == 16:
            for index, value in enumerate(struct.unpack(f">{amount}H", request[6:])):
                memory[unit, 3, address + index] = value
        elif function == 15:
            for index in range(amount):
                memory[unit, 1, address + index] = (request[6 + index // 8] >> (index % 8)) & 1
        return request[:5]
    return reply


def test_four_areas_and_unit_ids_with_real_tcp():
    async def exercise():
        async with tcp_peer(reply_from_memory({}), timeout_ms=1000) as (config, received):
            events = []
            session = Session(events.append)
            await session.connect(MODBUS, config, config.endpoint)
            for area, offset, values in (
                (Area.HR, 0, (0, 65535)),
                (Area.HR, 65535, (9,)),
                (Area.COIL, 0, (0, 1, 0)),
                (Area.COIL, 65535, (1,)),
            ):
                target = ModbusTarget(1, area, offset, len(values))
                await session.execute(Command(target, values))
                assert entries(events)[-1].outcome == "设备已应答"
                await session.execute(Command(target))
                assert tuple(point.value for point in readings(events)[-1].points) == values
            await session.execute(Command(ModbusTarget(2, Area.HR, 0, 1)))
            assert readings(events)[-1].points[0].value == 200
            await session.execute(Command(ModbusTarget(1, Area.IR, 0, 1)))
            assert readings(events)[-1].points[0].value == 100
            await session.execute(Command(ModbusTarget(1, Area.DI, 0, 9)))
            assert tuple(point.value for point in readings(events)[-1].points) == (1,) * 9
            assert [request[0] for _, request in received] == [16, 3, 6, 3, 15, 1, 5, 1, 3, 4, 2]
            assert received[8][0] == 2
            assert {direction for direction, _ in entries(events)[-1].packets} == {True, False}
            await session.disconnect()
            assert last_state(events).connection == ConnectionState.OFFLINE
    asyncio.run(exercise())


@pytest.mark.parametrize(("response", "kind", "outcome"), [
    (None, ErrorKind.TIMEOUT, "写入结果未知"),
    (b"\x06\x00\x00\x00\x01", ErrorKind.RESPONSE, "写入结果未知"),
    (b"\x03\x02\x00\x00", ErrorKind.RESPONSE, "写入结果未知"),
    (b"\x86\x02", ErrorKind.PROTOCOL, "协议异常"),
    (b"\x86\x0b", ErrorKind.PROTOCOL, "写入结果未知"),
    (False, ErrorKind.DISCONNECTED, "写入结果未知"),
])
def test_write_failures_do_not_resend(response, kind, outcome):
    async def exercise():
        async with tcp_peer(lambda _unit, _request: response) as (config, received):
            events = []
            session = Session(events.append)
            await session.connect(MODBUS, config, config.endpoint)
            write = asyncio.create_task(session.execute(Command(ModbusTarget(1, Area.HR, 0, 1), (42,))))
            if response is False:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(write, 2)
            else:
                await asyncio.wait_for(write, 2)
            writes = [entry for entry in entries(events) if entry.operation == "写入"]
            assert len(writes) == len(received) == 1
            assert (writes[0].error, writes[0].outcome) == (kind, outcome)
            await session.disconnect()
    asyncio.run(exercise())


def test_polling_stops_after_bad_read_and_keeps_last_result():
    async def exercise():
        requests = 0

        def reply(_unit, _request):
            nonlocal requests
            requests += 1
            return b"\x03\x02\x00\x19" if requests == 1 else b"\x03\x04\x00\x19\x00\x12"

        async with tcp_peer(reply) as (config, _):
            events = []
            session = Session(events.append)
            await session.connect(MODBUS, config, config.endpoint)
            await asyncio.wait_for(session.execute(Command(ModbusTarget(1, Area.HR, 0, 1)), 200), 2)
            first, second = readings(events)
            assert first.points[0].value == 25 and second.points is None
            assert second.entry.error == ErrorKind.RESPONSE
            assert not last_state(events).polling
            await session.disconnect()
    asyncio.run(exercise())


def test_disconnect_cancels_pending_write_and_records_outcome():
    async def exercise():
        received_write = asyncio.Event()

        def reply(_unit, _request):
            received_write.set()

        async with tcp_peer(reply, timeout_ms=3000) as (config, received):
            events = []
            session = Session(events.append)
            await session.connect(MODBUS, config, config.endpoint)
            task = asyncio.create_task(session.execute(Command(ModbusTarget(1, Area.HR, 0, 1), (42,))))
            await asyncio.wait_for(received_write.wait(), 2)
            await asyncio.wait_for(session.disconnect(), 2)
            assert task.cancelled()
            assert len(received) == 1
            assert entries(events)[-2].outcome == "写入结果未知"
            assert last_state(events).connection == ConnectionState.OFFLINE
    asyncio.run(exercise())


@pytest.mark.parametrize(("target", "values"), [
    (ModbusTarget(0, Area.HR, 0, 1), None),
    (ModbusTarget(1, Area.HR, 65535, 2), None),
    (ModbusTarget(1, Area.HR, 0, 126), None),
    (ModbusTarget(1, Area.IR, 0, 1), (1,)),
    (ModbusTarget(1, Area.HR, 0, 124), (0,) * 124),
    (ModbusTarget(1, Area.COIL, 0, 1969), (0,) * 1969),
    (ModbusTarget(1, Area.COIL, 0, 2001), None),
    (ModbusTarget(1, Area.COIL, 0, 1), (2,)),
])
def test_modbus_validation(target, values):
    with pytest.raises(ClientError) as caught:
        target.validate(values)
    assert caught.value.kind == ErrorKind.INPUT


def test_desktop_reads_from_a_tcp_peer(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from threading import Event
    from time import monotonic

    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from all_protocol.ui.io_bridge import IoBridge
    from all_protocol.ui.main_window import DesktopProtocol, MainWindow
    from all_protocol.ui.modbus_tcp_form import ModbusTcpForm

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    configurations = Queue()
    stop = Event()

    async def serve():
        async with tcp_peer(reply_from_memory({}), 1000) as (config, _received):
            configurations.put(config)
            await asyncio.to_thread(stop.wait)

    def wait_for(condition):
        deadline = monotonic() + 3
        while not condition() and monotonic() < deadline:
            QTest.qWait(10)
        assert condition()

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = executor.submit(asyncio.run, serve())
        bridge = IoBridge()
        form = ModbusTcpForm()
        window = MainWindow(bridge, (DesktopProtocol(MODBUS, lambda: form),))
        try:
            config = configurations.get(timeout=3)
            form.connection.host.setText(config.host)
            form.connection.port.setValue(config.port)
            window.show()
            window.connection_button.click()
            wait_for(window.read_button.isEnabled)
            assert window.write_button.isEnabled()
            form.query.area.setCurrentIndex(form.query.area.findData(Area.IR.value))
            assert not window.write_button.isEnabled()
            window.read_button.click()
            wait_for(lambda: window.results.rowCount() == 4 and window.read_button.isEnabled())
            assert window.results.items[0].value == 100
            window.log_table.selectRow(0)
            assert "TX  " in window.detail.toPlainText() and "RX  " in window.detail.toPlainText()
            window.close()
            wait_for(lambda: not window.isVisible())
        finally:
            bridge.join()
            window.close()
            stop.set()
            app.processEvents()
        server.result(timeout=3)
