import asyncio
import socket

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server import TcpServer
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Config, S7Target
from all_protocol.adapters.s7.server import S7Server
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import (
    ClientError, Command, ConnectionState, ErrorKind, Point, ProtocolRegistration, ReadUpdate, SessionState, TableReadUpdate,
)
from all_protocol.application.session import Session


@pytest.mark.parametrize("protocol", ("modbus-tcp", "s7"))
def test_table_read_uses_real_protocol_keeps_logs_and_continues_after_bad_point(protocol):
    async def run():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if protocol == "modbus-tcp":
            plan = ServerPlan(port=port, stations=(Station(1, "", (
                ServerPoint(Area.HR, 0, 235), ServerPoint(Area.HR, 4, 12.5, data_type=DataType.FLOAT32),
            )),))
            server = TcpServer(plan, lambda *_: None, lambda *_: None)
            targets = (ModbusTarget(1, Area.HR, 0, 1), ModbusTarget(1, Area.HR, 100, 1),
                       ModbusTarget(1, Area.HR, 4, 1, DataType.FLOAT32))
            registration = ProtocolRegistration(protocol, protocol, ModbusTcpClient, True)
            config = ModbusTcpConfig("127.0.0.1", port, 500)
        else:
            targets = (S7Target(S7Area.DB, 1, 0, S7Type.WORD), S7Target(S7Area.DB, 1, 100, S7Type.WORD),
                       S7Target(S7Area.DB, 1, 4, S7Type.REAL))
            plan = S7ServerPlan(port=port, points=(S7ServerPoint(targets[0], 235), S7ServerPoint(targets[2], 12.5)))
            server = S7Server(plan, lambda *_: None, lambda *_: None)
            registration = ProtocolRegistration(protocol, protocol, S7Client, True)
            config = S7Config("127.0.0.1", port, timeout_ms=500)
        events = []
        session = Session(events.append)
        try:
            await server.bind()
            await session.connect(registration, config, config.endpoint)
            await session.execute_table("table", tuple((str(index), Command(target)) for index, target in enumerate(targets)))
            updates = [event for event in events if isinstance(event, TableReadUpdate)]
            assert len(updates) == 3
            assert [event.point_id for event in updates] == ["0", "1", "2"]
            assert all(event.table_id == "table" for event in updates)
            assert updates[0].update.points[0].value == 235
            assert updates[1].update.entry.error is not None
            assert updates[2].update.points[0].value == 12.5
            assert all(event.update.entry.packets for event in updates)
            assert len({event.update.entry.request_id for event in updates}) == 3
            assert not any(isinstance(event, ReadUpdate) for event in events)
        finally:
            await session.disconnect()
            await server.shutdown()
    asyncio.run(run())


def test_disconnect_cancels_table_and_never_starts_remaining_points():
    async def run():
        started = asyncio.Event()
        calls, events = [], []

        class SlowClient:
            async def connect(self):
                pass

            async def close(self):
                calls.append("close")

            def function(self, command):
                return "read"

            async def read(self, target):
                calls.append(target.offset)
                started.set()
                await asyncio.Event().wait()

        session = Session(events.append)
        registration = ProtocolRegistration("slow", "Slow", lambda *_: SlowClient())
        await session.connect(registration, None, "slow")
        commands = tuple((str(offset), Command(ModbusTarget(1, Area.HR, offset, 1))) for offset in range(3))
        task = asyncio.create_task(session.execute_table("t", commands))
        await started.wait()
        await session.disconnect()
        await asyncio.gather(task, return_exceptions=True)
        assert calls == [0, "close"]
        update = next(event for event in events if isinstance(event, TableReadUpdate))
        assert update.update.entry.error == ErrorKind.DISCONNECTED
        final = [event for event in events if isinstance(event, SessionState)][-1]
        assert final.connection == ConnectionState.OFFLINE and not final.busy
    asyncio.run(run())


@pytest.mark.parametrize("count", (0, 2))
def test_scalar_response_count_errors_are_logged_and_writes_are_rejected(count):
    async def run():
        events, reads = [], []

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, _command):
                return "read"

            async def read(self, target):
                reads.append(target)
                return tuple(Point(str(index), "value", index) for index in range(count))

        session = Session(events.append)
        target = ModbusTarget(1, Area.HR, 0, 1)
        await session.connect(ProtocolRegistration("test", "Test", lambda *_: Client()), None, "test")
        try:
            with pytest.raises(ClientError, match="写请求"):
                await session.execute_table("t", (("p", Command(target, (1,))),))
            assert not reads
            await session.execute_table("t", (("p", Command(target)),))
            update = next(event for event in events if isinstance(event, TableReadUpdate))
            assert update.update.entry.error == ErrorKind.RESPONSE
        finally:
            await session.disconnect()
    asyncio.run(run())
