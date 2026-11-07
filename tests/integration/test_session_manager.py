import asyncio
from dataclasses import dataclass
import socket

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server import TcpServer
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Config, S7Target
from all_protocol.adapters.s7.server import S7Server
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import (
    BatchResult, ClientError, Command, ConnectionState, DeviceEvent, DeviceRemoved, ErrorKind,
    JournalEntry, OperationRejected, Point, ProtocolRegistration, SessionState, TableReadUpdate,
)
from all_protocol.application.session_manager import SessionManager


@dataclass(frozen=True)
class Target:
    label: str


def test_real_modbus_and_s7_connections_have_independent_identity_and_resources():
    async def run():
        ports = []
        for _ in range(2):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                ports.append(probe.getsockname()[1])
        modbus = TcpServer(ServerPlan(port=ports[0], stations=(Station(1, "", (ServerPoint(Area.HR, 0, 235),)),)),
                           lambda *_: None, lambda *_: None)
        target = S7Target(S7Area.DB, 1, 0, S7Type.WORD)
        s7 = S7Server(S7ServerPlan(port=ports[1], points=(S7ServerPoint(target, 125),)), lambda *_: None, lambda *_: None)
        events = []
        manager = SessionManager(events.append)
        configs = ModbusTcpConfig("127.0.0.1", ports[0]), S7Config("127.0.0.1", ports[1])
        specs = (("modbus", ProtocolRegistration("modbus-tcp", "Modbus", ModbusTcpClient), configs[0], configs[0].endpoint),
                 ("s7", ProtocolRegistration("s7", "S7", S7Client), configs[1], configs[1].endpoint))
        try:
            await modbus.bind()
            await s7.bind()
            result = await manager.connect_all(specs)
            assert all(item.successful for item in result.results)
            await asyncio.gather(manager.for_device("modbus").execute_table("table", (("point", Command(ModbusTarget(1, Area.HR, 0, 1))),)),
                                 manager.for_device("s7").execute_table("table", (("point", Command(target)),)))
            updates = [event for event in events if isinstance(event.payload, TableReadUpdate)]
            assert {event.device_id: event.payload.update.points[0].value for event in updates} == {"modbus": 235, "s7": 125}
            assert len({event.session_id for event in updates}) == 2
            assert {event.generation for event in updates} == {1}
            assert {event.payload.update.entry.request_id for event in updates} == {2}
            assert all(event.payload.update.entry.device_id == event.device_id
                       and event.payload.update.entry.session_id == event.session_id for event in updates)
            await manager.disconnect("modbus")
            assert manager.devices["modbus"].state.connection == ConnectionState.OFFLINE
            assert manager.devices["s7"].state.connection == ConnectionState.ONLINE
            assert await manager.for_device("s7").execute(Command(target))
            await manager.shutdown()
            await modbus.shutdown()
            await s7.shutdown()
            for port in ports:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
        finally:
            await manager.shutdown()
            await modbus.shutdown()
            await s7.shutdown()
    asyncio.run(run())


def test_batch_partial_failure_does_not_cancel_good_device_and_reconnect_gets_new_id():
    async def run():
        closed, events = [], []

        class Client:
            def __init__(self, config, _events):
                self.config = config

            async def connect(self):
                if self.config == "bad":
                    raise ClientError(ErrorKind.CONNECT, "模拟连接失败")

            async def close(self):
                closed.append(self.config)

        manager = SessionManager(events.append)
        protocol = ProtocolRegistration("fake", "Fake", Client)
        result = await manager.connect_all((("a", protocol, "good", "one"), ("b", protocol, "bad", "two")))
        assert [item.successful for item in result.results] == [True, False]
        assert "模拟连接失败" in result.results[1].detail
        first = manager.devices["a"].session_id
        assert not await manager.connect("a", protocol, "changed", "changed")
        assert manager.devices["a"].session_id == first
        await manager.disconnect("a")
        assert await manager.connect("a", protocol, "good", "one")
        assert manager.devices["a"].session_id != first
        assert manager.devices["a"].state.generation == 3
        await manager.shutdown()
        assert closed.count("good") == 2 and closed.count("bad") == 1
    asyncio.run(run())


def test_blocked_device_cannot_block_other_device_requests_or_shutdown():
    async def run():
        blocked = asyncio.Event()
        events, closed = [], []

        class Client:
            def __init__(self, config, _events):
                self.config = config

            async def connect(self):
                pass

            async def close(self):
                closed.append(self.config)

            def function(self, command):
                return "read"

            async def read(self, target):
                if self.config == "slow":
                    blocked.set()
                    await asyncio.Event().wait()
                return (Point("p", "value", 42),)

        manager = SessionManager(events.append)
        protocol = ProtocolRegistration("fake", "Fake", Client)
        await manager.connect_all((("a", protocol, "slow", "one"), ("b", protocol, "fast", "two")))
        slow = asyncio.create_task(manager.for_device("a").execute(Command(Target("slow"))))
        await blocked.wait()
        assert await asyncio.wait_for(manager.for_device("b").execute(Command(Target("fast"))), 1)
        await asyncio.wait_for(manager.shutdown(), 1)
        await asyncio.gather(slow, return_exceptions=True)
        assert sorted(closed) == ["fast", "slow"] and not manager.devices
    asyncio.run(run())


@pytest.mark.parametrize("started", (False, True))
def test_disconnect_during_owned_handshake_is_bounded(started):
    async def run():
        connecting = asyncio.Event()
        closed = []

        class Client:
            def __init__(self, *_args):
                pass

            async def connect(self):
                connecting.set()
                await asyncio.Event().wait()

            async def close(self):
                closed.append(True)

        manager = SessionManager(lambda *_: None)
        connect = asyncio.create_task(manager.connect("a", ProtocolRegistration("fake", "Fake", Client), None, "one"))
        if started:
            await connecting.wait()
            await asyncio.wait_for(manager.disconnect_all(), 1)
        else:
            await asyncio.sleep(0)
            await manager.disconnect("a")
        assert await asyncio.wait_for(connect, 1) is False
        assert manager.devices["a"].state.connection == ConnectionState.OFFLINE
        await manager.shutdown()
        assert len(closed) == int(started)
    asyncio.run(run())


def test_removed_runtime_cannot_forward_delayed_callbacks_or_resurrect_via_commands():
    async def run():
        events = []

        class Client:
            def __init__(self, *_args):
                pass

            async def connect(self):
                pass

            async def close(self):
                pass

        manager = SessionManager(events.append)
        protocol = ProtocolRegistration("fake", "Fake", Client)
        await manager.connect("a", protocol, None, "one")
        retired = manager.devices["a"]
        await manager.remove("a")
        assert isinstance(events[-1].payload, DeviceRemoved)
        length = len(events)
        retired.session._emit(SessionState(ConnectionState.ONLINE, generation=999))
        assert len(events) == length
        assert not await manager.for_device("a").execute(Command(Target("x")))
        assert not manager.devices
        await manager.connect("a", protocol, None, "one")
        assert manager.devices["a"].runtime_id != retired.runtime_id
        await manager.shutdown()
    asyncio.run(run())


def test_shutdown_rejects_new_admission_while_clients_are_closing():
    async def run():
        closing, release = asyncio.Event(), asyncio.Event()

        class Client:
            def __init__(self, *_args):
                pass

            async def connect(self):
                pass

            async def close(self):
                closing.set()
                await release.wait()

        manager = SessionManager(lambda *_: None)
        protocol = ProtocolRegistration("fake", "Fake", Client)
        await manager.connect("a", protocol, None, "one")
        shutdown = asyncio.create_task(manager.shutdown())
        await closing.wait()
        assert not await manager.connect("b", protocol, None, "two")
        assert not await manager.for_device("a").execute(Command(Target("p")))
        release.set()
        await shutdown
        assert not manager.devices
    asyncio.run(run())
