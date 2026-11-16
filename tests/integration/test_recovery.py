import asyncio
from dataclasses import dataclass

import pytest

from all_protocol.application.contracts import (
    ClientError, Command, ConnectionState, ErrorKind, JournalEntry, Point,
    ProtocolRegistration, TableReadUpdate, WriteStatus,
)
from all_protocol.application.recovery import RecoveryPolicy
from all_protocol.application.session import Session
from all_protocol.application.session_manager import SessionManager


@dataclass(frozen=True)
class Target:
    label: str = "value"


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class Rig:
    def __init__(self):
        self.clients = []
        self.events = []
        self.writes = []
        self.failures = []
        self.block_write = False
        self.block_connect = False

        rig = self

        class Client:
            write_sent = True

            def __init__(self, config, events):
                self.config, self.events = config, events
                self.closed = False
                self.notifications = []
                rig.clients.append(self)

            async def connect(self):
                if rig.block_connect:
                    await asyncio.Event().wait()
                if rig.failures:
                    raise rig.failures.pop(0)

            async def close(self):
                self.closed = True

            def function(self, command):
                return "write" if command.values is not None else "read"

            async def read(self, target):
                return (Point(target.label, "int", 42),)

            async def write(self, target, values):
                rig.writes.append(values)
                if rig.block_write:
                    await asyncio.Event().wait()

            async def subscribe(self, targets, interval, queue, notify):
                self.notifications.append(notify)

                class Subscription:
                    async def close(self):
                        pass

                return Subscription()

        self.protocol = ProtocolRegistration("fake", "Fake", Client, True)
        self.session = Session(self.events.append)
        self.commands = (("point", Command(Target())),)
        self.policy = RecoveryPolicy(True, 200, 200, 2, True)


def test_retry_failure_limit_and_manual_restart_has_new_identity():
    async def run():
        rig = Rig()
        rig.failures = [ClientError(ErrorKind.CONNECT, "unavailable") for _ in range(3)]
        await rig.session.connect(rig.protocol, "config", "device", rig.policy)
        await until(lambda: "已达" in rig.session._state.recovery_error)
        assert len(rig.clients) == 3 and all(client.closed for client in rig.clients)
        assert rig.session._state.connection == ConnectionState.OFFLINE
        assert rig.session._state.next_retry_at is None
        failed_id = rig.session._state.session_id
        await rig.session.connect(rig.protocol, "config", "device", rig.policy)
        assert rig.session._state.connection == ConnectionState.ONLINE
        assert rig.session._state.session_id != failed_id
        assert rig.session._state.recovery_attempt == 0
        await rig.session.disconnect()
    asyncio.run(run())


def test_initial_failure_enters_waiting_atomically_and_retry_keeps_original_configuration():
    async def run():
        rig = Rig()
        manager = SessionManager(lambda event: None)
        rig.failures = [ClientError(ErrorKind.CONNECT, "temporary")]
        assert not await manager.connect("a", rig.protocol, "original", "original", rig.policy)
        assert manager.devices["a"].state.connection == ConnectionState.WAITING
        assert not await manager.connect("a", rig.protocol, "changed", "changed", rig.policy)
        await until(lambda: manager.devices["a"].state.connection == ConnectionState.ONLINE)
        assert [client.config for client in rig.clients] == ["original", "original"]
        assert manager.devices["a"].config_key == (rig.protocol.key, "original", "original")
        await manager.shutdown()
    asyncio.run(run())


def test_recovery_restores_reads_but_never_unknown_or_queued_writes():
    async def run():
        rig = Rig()
        session = rig.session
        await session.connect(rig.protocol, "config", "device", rig.policy)
        await session.start_table_polling("poll", rig.commands, 200)
        await session.start_table_subscription("sub", rig.commands, 200)
        quick = asyncio.create_task(session.execute(Command(Target("quick")), 200))
        await until(lambda: session._state.polling)
        await until(lambda: any(isinstance(event, TableReadUpdate) for event in rig.events))
        old_id, old_generation = session._state.session_id, session._generation
        rig.block_write = True
        write = asyncio.create_task(session.execute(Command(Target(), (1,))))
        await until(lambda: bool(rig.writes))
        queued = asyncio.create_task(session.execute(Command(Target(), (2,))))
        await until(lambda: session._state.queued == 2)
        old = rig.clients[0]
        old.events.disconnected()
        old.events.disconnected()  # Duplicate loss callbacks must not create another retry owner.
        await until(lambda: len(rig.clients) == 2 and session._state.subscription_groups == ("sub",))
        assert session._state.polling_groups == ("poll",)
        assert session._state.polling
        assert session._state.session_id != old_id and session._generation > old_generation
        assert rig.writes == [(1,)]
        statuses = [event.write.status for event in rig.events if isinstance(event, JournalEntry) and event.write]
        assert statuses == [WriteStatus.UNKNOWN, WriteStatus.NOT_SENT]
        count = len([event for event in rig.events if isinstance(event, TableReadUpdate)])
        old.notifications[0](0, Point("old", "int", 999), "retired")
        old.events.disconnected()
        assert len([event for event in rig.events if isinstance(event, TableReadUpdate)]) == count
        assert session._state.connection == ConnectionState.ONLINE
        await asyncio.gather(write, queued, quick, return_exceptions=True)
        await session.disconnect()
        assert session._recovery_task is None and all(client.closed for client in rig.clients)
    asyncio.run(run())


@pytest.mark.parametrize("restore", (False, True))
def test_waiting_stop_all_prevents_resurrection_and_waiting_disconnect_cancels_retry(restore):
    async def run():
        rig = Rig()
        policy = RecoveryPolicy(True, 200, 200, 2, restore)
        await rig.session.connect(rig.protocol, "config", "device", policy)
        await rig.session.start_table_polling("table", rig.commands, 200)
        await rig.session.start_table_subscription("sub", rig.commands, 200)
        rig.clients[0].events.disconnected()
        await until(lambda: rig.session._state.connection == ConnectionState.WAITING)
        assert bool(rig.session._state.recovering_tables) == restore
        await rig.session.stop_all_polling()
        await until(lambda: len(rig.clients) == 2 and rig.session._state.connection == ConnectionState.ONLINE)
        assert not rig.session._state.polling_groups and not rig.session._state.subscription_groups
        rig.clients[-1].events.disconnected()
        await until(lambda: rig.session._state.connection == ConnectionState.WAITING)
        await rig.session.disconnect()
        await asyncio.sleep(.25)
        assert len(rig.clients) == 2 and rig.session._state.connection == ConnectionState.OFFLINE
        assert rig.session._recovery_task is None and not rig.session._state.recovering_tables
    asyncio.run(run())


def test_default_policy_does_not_retry_or_restore_subscriptions():
    async def run():
        rig = Rig()
        await rig.session.connect(rig.protocol, "config", "device")
        await rig.session.start_table_subscription("table", rig.commands, 200)
        await rig.session.disconnect(unexpected=True)
        assert rig.session._recovery_task is None
        await rig.session.connect(rig.protocol, "config", "device")
        assert not rig.session._state.subscription_groups
        await rig.session.disconnect()
    asyncio.run(run())


@pytest.mark.parametrize("failure", (
    ClientError(ErrorKind.INPUT, "invalid config"),
    ClientError(ErrorKind.CONNECT, "certificate rejected", code=0x801A0000),
))
def test_configuration_or_certificate_failure_stops_recovery(failure):
    async def run():
        rig = Rig()
        rig.failures = [ClientError(ErrorKind.CONNECT, "temporary"), failure]
        await rig.session.connect(rig.protocol, "config", "device", rig.policy)
        await until(lambda: "恢复停止" in rig.session._state.recovery_error)
        assert len(rig.clients) == 2 and rig.session._state.connection == ConnectionState.OFFLINE
        assert rig.session._state.next_retry_at is None
        await rig.session.disconnect()
    asyncio.run(run())


def test_shutdown_during_retry_handshake_and_other_device_independence():
    async def run():
        rig = Rig()
        manager = SessionManager(lambda event: None)
        await manager.connect("a", rig.protocol, "one", "one", rig.policy)
        await manager.connect("b", rig.protocol, "two", "two")
        rig.clients[0].events.disconnected()
        await until(lambda: manager.devices["a"].state.connection == ConnectionState.WAITING)
        assert await manager.for_device("b").execute(Command(Target()))
        rig.block_connect = True
        await until(lambda: len(rig.clients) == 3)
        await asyncio.wait_for(manager.shutdown(), 1)
        assert all(client.closed for client in rig.clients) and not manager.devices
    asyncio.run(run())


def test_removing_waiting_device_cancels_retry_and_old_callbacks():
    async def run():
        rig = Rig()
        manager = SessionManager(lambda event: None)
        await manager.connect("a", rig.protocol, "one", "one", rig.policy)
        rig.clients[0].events.disconnected()
        await until(lambda: manager.devices["a"].state.connection == ConnectionState.WAITING)
        await manager.remove("a")
        rig.clients[0].events.disconnected()
        await asyncio.sleep(.25)
        assert len(rig.clients) == 1 and not manager.devices
        await manager.shutdown()
    asyncio.run(run())


def test_real_tcp_server_restart_restores_table_on_new_session():
    import socket
    from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
    from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
    from all_protocol.adapters.modbus_tcp.server import TcpServer
    from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station

    async def run():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        plan = ServerPlan(port=port, stations=(Station(1, "", (ServerPoint(Area.HR, 0, 42),)),))
        server = TcpServer(plan, lambda *_: None, lambda *_: None)
        events = []
        session = Session(events.append)
        config = ModbusTcpConfig("127.0.0.1", port)
        registration = ProtocolRegistration("modbus-tcp", "Modbus", ModbusTcpClient, True)
        try:
            await server.bind()
            await session.connect(registration, config, config.endpoint, RecoveryPolicy(True, 200, 200, 5, True))
            await session.start_table_polling("table", (("point", Command(ModbusTarget(1, Area.HR, 0, 1))),), 200)
            await until(lambda: any(isinstance(event, TableReadUpdate) and event.update.points for event in events))
            previous = session._state.session_id
            await server.shutdown()
            await until(lambda: session._state.connection == ConnectionState.WAITING)
            server = TcpServer(plan, lambda *_: None, lambda *_: None)
            await server.bind()
            await until(lambda: any(isinstance(event, TableReadUpdate) and event.update.entry.session_id != previous
                                    and event.update.points and event.update.points[0].value == 42 for event in events))
            assert session._state.polling_groups == ("table",)
            assert session._state.session_id != previous
        finally:
            await session.disconnect()
            await server.shutdown()
    asyncio.run(run())
