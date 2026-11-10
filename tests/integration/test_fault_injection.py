import asyncio
import socket

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, ServerPoint, Station
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType, S7Config, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import Command, ErrorKind, JournalEntry, ProtocolRegistration
from all_protocol.application.faults import FaultAction, FaultOperation, FaultRule, FaultUpdate
from all_protocol.application.server_contracts import RequestMetadata
from all_protocol.application.server_session import ServerSession
from all_protocol.application.session import Session
from all_protocol.server_protocols import SERVER_PROTOCOLS


@pytest.mark.parametrize("index", (0, 1), ids=("modbus", "s7"))
def test_fault_templates_apply_to_real_requests_and_stop_cancels_delayed_write(index, tmp_path):
    async def run():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if index == 0:
            plan = ServerPlan(port=port, stations=(Station(1, points=(ServerPoint(Area.HR, 0, 7),)),))
            config = ModbusTcpConfig(plan.host, port, 2000)
            target = ModbusTarget(1, Area.HR, 0, 1)
            server_target = ModbusServerTarget(1, Area.HR, 0)
            create_client = ModbusTcpClient
        else:
            target = server_target = S7Target(S7Area.DB, 1, 0, DataType.WORD)
            plan = S7ServerPlan(port=port, points=(S7ServerPoint(target, 7),))
            config = S7Config(plan.host, port, timeout_ms=2000)
            create_client = S7Client
        protocol = SERVER_PROTOCOLS[index]
        registration = ProtocolRegistration(protocol.registration.key, protocol.registration.label, create_client, True)
        server_events, client_events = [], []
        server = ServerSession(server_events.append, protocol.registration)
        client = Session(client_events.append)

        def latest(operation=None):
            return next(entry for entry in reversed(client_events)
                        if isinstance(entry, JournalEntry) and (operation is None or entry.operation == operation))

        def status():
            return next(event.payload for event in reversed(server_events) if isinstance(event.payload, FaultUpdate))

        await server.start(plan)
        try:
            await client.connect(registration, config, config.endpoint)
            first_session = latest().session_id
            await server.configure_fault(FaultRule(FaultAction.EXCEPTION, FaultOperation.WRITE))
            await client.execute(Command(target))
            assert latest().error is None and status().triggered == 0
            await client.execute(Command(target, (9,)))
            assert latest().error == ErrorKind.PROTOCOL and latest().outcome != "写入结果未知"
            assert latest().session_id == first_session
            assert await server.read_current(server_target) == 7
            await client.execute(Command(target, (11,)))
            assert latest().error is None and status().triggered == 1 and not status().active

            await server.configure_fault(FaultRule(FaultAction.DISCONNECT, FaultOperation.WRITE))
            await asyncio.gather(client.execute(Command(target, (12,))), return_exceptions=True)
            assert latest("写入").error == ErrorKind.DISCONNECTED and latest("写入").outcome == "写入结果未知"
            assert await server.read_current(server_target) == 11
            await client.disconnect()
            await client.connect(registration, config, config.endpoint)
            assert latest().session_id and latest().session_id != first_session

            await server.configure_fault(FaultRule(FaultAction.DELAY, FaultOperation.WRITE, delay_ms=60000))
            writing = asyncio.create_task(client.execute(Command(target, (13,))))
            async with asyncio.timeout(1):
                while await server.read_current(server_target) != 13:
                    await asyncio.sleep(.005)
            assert not writing.done()
            await asyncio.wait_for(server.stop(), 1)
            await asyncio.wait_for(asyncio.gather(writing, return_exceptions=True), 1)
            assert latest("写入").outcome == "写入结果未知" and status() == FaultUpdate()
            events = [event for event in server_events if isinstance(event.payload, RequestMetadata)]
            assert len({(event.payload.session_id, event.payload.request_id) for event in events}) == len(events)
            stopped_delay = events[-1]
            assert stopped_delay.payload.error == ErrorKind.DISCONNECTED
            assert stopped_delay.payload.elapsed_ms is not None and stopped_delay.payload.rx and not stopped_delay.payload.tx
            storage = protocol.create_storage(tmp_path)
            storage.append_event(stopped_delay)
            assert storage.recent_events() == (stopped_delay,)
            await server.start(plan)
            await client.disconnect()
            await client.connect(registration, config, config.endpoint)
            await client.execute(Command(target))
            assert latest().error is None
        finally:
            await client.disconnect()
            await server.stop()

    asyncio.run(run())


def test_modbus_pipelined_frames_delay_only_the_matching_transaction():
    async def run():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        plan = ServerPlan(port=port, stations=(Station(1, points=(ServerPoint(Area.HR, 0, 7),)),))
        server = ServerSession(lambda _: None, SERVER_PROTOCOLS[0].registration)
        await server.start(plan)
        reader, writer = await asyncio.open_connection(plan.host, port)
        try:
            await server.configure_fault(FaultRule(FaultAction.DELAY, delay_ms=60000))
            writer.write(bytes.fromhex("000100000006010300000001 000200000006010300000001"))
            await writer.drain()
            second = await asyncio.wait_for(reader.readexactly(11), 1)
            assert second == bytes.fromhex("0002000000050103020007")
            await server.configure_fault(None)
            first = await asyncio.wait_for(reader.readexactly(11), 1)
            assert first == bytes.fromhex("0001000000050103020007")
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    asyncio.run(run())
