import asyncio
import socket
from contextlib import asynccontextmanager

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Config, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import (
    Command, DeviceEvent, ErrorKind, JournalEntry, ProtocolRegistration, VerificationOptions, VerificationStatus, WriteStatus,
)
from all_protocol.application.faults import FaultAction, FaultOperation, FaultRule
from all_protocol.application.server_contracts import RequestMetadata
from all_protocol.application.server_session import ServerSession
from all_protocol.application.session_manager import SessionManager
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import LocalHistory


@asynccontextmanager
async def bench(index, *, floating=False):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if index == 0:
        kind = DataType.FLOAT32 if floating else None
        target = ModbusTarget(1, Area.HR, 0, 1, kind)
        server_target = ModbusServerTarget(1, Area.HR, 0)
        plan = ServerPlan(port=port, stations=(Station(1, points=(ServerPoint(Area.HR, 0, 0, data_type=kind),)),))
        config = ModbusTcpConfig(plan.host, port, timeout_ms=250)
        create_client, read_code, write_code = ModbusTcpClient, 3, 16 if floating else 6
    else:
        kind = S7Type.REAL if floating else S7Type.WORD
        target = server_target = S7Target(S7Area.DB, 1, 0, kind)
        plan = S7ServerPlan(port=port, points=(S7ServerPoint(target, 0),))
        config = S7Config(plan.host, port, timeout_ms=250)
        create_client, read_code, write_code = S7Client, 4, 5
    protocol = SERVER_PROTOCOLS[index].registration
    registration = ProtocolRegistration(protocol.key, protocol.label, create_client, True)
    server_events, entries = [], []
    acknowledged = asyncio.Event()

    def emit(event: DeviceEvent):
        if isinstance(event.payload, JournalEntry):
            entries.append(event.payload)
            if event.payload.write is not None and event.payload.write.status == WriteStatus.ACKNOWLEDGED:
                acknowledged.set()

    def operations():
        codes = [dict(event.payload.details).get("function") for event in server_events
                 if isinstance(event.payload, RequestMetadata)]
        return ["write" if code == write_code else "read" for code in codes if code in (read_code, write_code)]

    server = ServerSession(server_events.append, protocol)
    manager = SessionManager(emit)
    client = manager.for_device("a")
    connection = (registration, config, config.endpoint)
    await server.start(plan)
    try:
        assert await client.connect(*connection)
        yield server, client, target, server_target, entries, operations, acknowledged, connection
    finally:
        await manager.shutdown()
        await server.stop()


@pytest.mark.parametrize("index", (0, 1), ids=("modbus", "s7"))
def test_automatic_verification_is_adjacent_and_uses_encoded_float(index, tmp_path):
    async def run():
        async with bench(index, floating=True) as (server, client, target, server_target, entries, ops, ack, _):
            await client.execute(Command(target, (.1,), VerificationOptions(0, 0, 0)))
            write, read = entries[-2:]
            assert write.write.status == WriteStatus.ACKNOWLEDGED
            assert write.write.values[0] != .1 and write.write.values == read.verification.observed
            assert read.verification.status == VerificationStatus.MATCH
            assert any(not sending for sending, _ in write.packets)
            assert read.verification.write_session_id == write.session_id == read.session_id
            assert read.verification.write_request_id == write.request_id
            assert read.operation_id == write.operation_id

            ack.clear()
            task = asyncio.create_task(client.execute(Command(target, (.5,), VerificationOptions(100, 0, 0))))
            await asyncio.wait_for(ack.wait(), 1)
            queued = asyncio.create_task(client.execute(Command(target)))
            await server.write_current(server_target, .75)
            assert await task  # Write acknowledgement is independent of the value comparison.
            await queued
            write, read, ordinary = entries[-3:]
            assert ordinary.verification is None
            assert read.verification.status == VerificationStatus.MISMATCH
            assert (read.verification.expected, read.verification.observed, read.verification.mismatches) == ((.5,), (.75,), (0,))
            assert ops() == ["write", "read", "write", "read", "read"]
            history = LocalHistory(tmp_path)
            history.append_entry(write)
            history.append_entry(read)
            assert history.recent_entries() == (read, write)

    asyncio.run(run())


@pytest.mark.parametrize("index", (0, 1), ids=("modbus", "s7"))
def test_rejection_unknown_write_and_read_timeout_never_resend(index, tmp_path):
    async def run():
        async with bench(index) as (server, client, target, server_target, entries, ops, _, connection):
            options = VerificationOptions()
            await server.configure_fault(FaultRule(FaultAction.EXCEPTION, FaultOperation.WRITE))
            assert not await client.execute(Command(target, (3,), options))
            assert entries[-1].write.status == WriteStatus.REJECTED
            assert ops() == ["write"] and await server.read_current(server_target) == 0

            await server.configure_fault(FaultRule(FaultAction.DELAY, FaultOperation.WRITE, delay_ms=5000))
            await asyncio.gather(client.execute(Command(target, (42,), options)), return_exceptions=True)
            unknown = next(entry for entry in reversed(entries) if entry.write is not None)
            assert unknown.error == ErrorKind.TIMEOUT and unknown.write.status == WriteStatus.UNKNOWN
            assert await server.read_current(server_target) == 42
            assert not any(entry.verification for entry in entries)
            await client.disconnect()
            await server.configure_fault(None)
            assert await client.connect(*connection)
            history = LocalHistory(tmp_path)
            history.append_entry(unknown)
            restored, = history.recent_entries()
            assert restored == unknown
            assert await client.verify_write(restored)
            check = entries[-1]
            assert check.verification.independent and check.verification.status == VerificationStatus.MATCH
            assert check.verification.write_session_id == unknown.session_id != check.session_id
            assert check.verification.write_request_id == unknown.request_id

            await server.configure_fault(FaultRule(FaultAction.DELAY, FaultOperation.READ, delay_ms=5000))
            await asyncio.gather(client.execute(Command(target, (19,), options)), return_exceptions=True)
            write = next(entry for entry in reversed(entries) if entry.write is not None)
            failed = next(entry for entry in reversed(entries) if entry.verification is not None)
            assert write.write.status == WriteStatus.ACKNOWLEDGED
            assert failed.error == ErrorKind.TIMEOUT and failed.verification.status == VerificationStatus.UNAVAILABLE
            assert failed.verification.write_request_id == write.request_id
            assert await server.read_current(server_target) == 19
            await client.disconnect()
            await server.configure_fault(None)
            assert ops().count("write") == 3

    asyncio.run(run())
