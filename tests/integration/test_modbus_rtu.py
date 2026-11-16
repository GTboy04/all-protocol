"""Exercise real PyModbus RTU framing over a deterministic serial cable substitute."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime

import pytest
import serial
from pymodbus.framer.rtu import FramerRTU

from all_protocol.adapters.modbus_rtu.client import ModbusRtuClient
from all_protocol.adapters.modbus_rtu.model import ModbusRtuConfig, ModbusRtuTarget, normalize_port
from all_protocol.adapters.modbus_rtu.ports import acquire, release
from all_protocol.adapters.modbus_rtu.server_adapter import RtuServerAdapter
from all_protocol.adapters.modbus_rtu.server_model import RtuServerPlan
from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import Station, ServerPoint, ModbusServerTarget
from all_protocol.application.contracts import ClientError, ClientEvents, ErrorKind, Command, JournalEntry, ProtocolRegistration, WriteStatus
from all_protocol.application.packet_decoder import decode_packet
from all_protocol.application.server_contracts import ServerCallbacks
from all_protocol.application.session import Session
from all_protocol.point_tables import PointDefinition, PointTable, export_table_json, import_table_json, export_points_csv, import_points_csv
from all_protocol.project import Project, ProjectFile, project_record, decode_project
from all_protocol.storage import RtuServerStorage, ConnectionProfile, LocalHistory


@pytest.fixture
def cable(monkeypatch):
    ports = {}
    traffic = []

    class Port:
        def __init__(self, name, **kwargs):
            if name in ports and ports[name].is_open:
                raise serial.SerialException("busy")
            self.name, self.is_open = name, True
            self.data = bytearray()
            ports[name] = self
        @property
        def in_waiting(self):
            return len(self.data)
        def read(self, size):
            result = bytes(self.data[:size])
            del self.data[:size]
            return result
        def write(self, data):
            traffic.append((self.name, bytes(data)))
            peer = ports.get("COM2" if self.name == "COM1" else "COM1")
            if peer and peer.is_open:
                peer.data.extend(data)
            return len(data)
        def close(self):
            self.is_open = False
        def reset_input_buffer(self):
            self.data.clear()
    monkeypatch.setattr(serial, "serial_for_url", Port)
    return ports, traffic


def plan():
    return RtuServerPlan(stations=(Station(1, points=(ServerPoint(Area.HR, 0, 7), ServerPoint(Area.COIL, 0, 1))),
                                   Station(2, points=(ServerPoint(Area.HR, 0, 42),))))


def test_rtu_real_framing_read_write_timeout_isolation_and_release(cable):
    async def run():
        records, packets = [], []
        server = RtuServerAdapter(plan(), ServerCallbacks(lambda time, request: records.append(request), lambda change: None))
        client = ModbusRtuClient(ModbusRtuConfig(timeout_ms=100), ClientEvents(lambda sending, data: packets.append((sending, data)), lambda: None))
        await server.bind()
        try:
            await client.connect()
            for area in (Area.HR, Area.COIL):
                target = ModbusRtuTarget(1, area, 0, 1)
                assert (await client.read(target))[0].value == (7 if area == Area.HR else 1)
                await client.write(target, (9 if area == Area.HR else 0,))
                assert (await client.read(target))[0].value == (9 if area == Area.HR else 0)
            with pytest.raises(ClientError) as missing:
                await client.read(ModbusRtuTarget(3, Area.HR, 0, 1))
            assert missing.value.kind == ErrorKind.TIMEOUT
            with pytest.raises(ClientError) as paused:
                await client.read(ModbusRtuTarget(3, Area.HR, 0, 1))
            assert paused.value.kind == ErrorKind.INPUT
            assert (await client.read(ModbusRtuTarget(2, Area.HR, 0, 1)))[0].value == 42
            assert records and all(not item.error for item in records)
            assert packets and all(FramerRTU.check_CRC(data[:-2], int.from_bytes(data[-2:], "big")) for _, data in packets)
        finally:
            await client.close()
            await client.close()
            await server.shutdown()
            await server.shutdown()
        assert all(not port.is_open for port in cable[0].values())
        owner = object()
        acquire("COM1", owner)
        release("COM1", owner)
    asyncio.run(run())


def test_unknown_write_and_evidence_roundtrip(cable, tmp_path):
    async def run():
        events = []
        session = Session(events.append)
        config = ModbusRtuConfig(timeout_ms=100)
        registration = ProtocolRegistration("modbus-rtu", "RTU", ModbusRtuClient, True)
        await session.connect(registration, config, config.endpoint)
        try:
            await session.execute(Command(ModbusRtuTarget(1, Area.HR, 0, 1), (8,)))
            entry = next(event for event in reversed(events) if isinstance(event, JournalEntry) and event.write)
            assert entry.write.status == WriteStatus.UNKNOWN
            history = LocalHistory(tmp_path)
            history.append_entry(entry)
            assert history.recent_entries()[-1] == entry
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_ownership_normalization_and_failed_open_releases(monkeypatch):
    async def run():
        owner = object()
        acquire("com1", owner)
        client = ModbusRtuClient(ModbusRtuConfig(), ClientEvents(lambda *args: None, lambda: None))
        try:
            with pytest.raises(ClientError):
                await client.connect()
            with pytest.raises(OSError):
                acquire("COM1", object())
        finally:
            release("COM1", owner)
        async def failed():
            raise OSError("missing port")
        monkeypatch.setattr(client._client, "connect", failed)
        with pytest.raises(ClientError):
            await client.connect()
        acquire("COM1", owner)
        release("COM1", owner)
    asyncio.run(run())
    assert normalize_port(r"\\.\com12") == "COM12"


def test_rtu_project_point_tables_storage_and_legacy(tmp_path):
    config = ModbusRtuConfig()
    target = ModbusRtuTarget(1, Area.HR, 0, 1)
    fields = dict(asdict(config), unit_id=1, area="HR", offset=0, count=1, data_type="UInt16", order="big")
    project = Project("RTU", rtu=plan()).remember(ConnectionProfile("modbus-rtu", "COM1", fields, 1000))
    table = PointTable("table", project.devices[0].id, "points", (PointDefinition("point", "value", target),))
    project = project.with_table(table)
    file = ProjectFile()
    path = tmp_path / "rtu.allprotocol"
    file.save(project, path)
    assert file.open(path) == project
    assert import_table_json(export_table_json(table, "modbus-rtu"), "modbus-rtu", "other").points[0].target == table.points[0].target
    assert import_points_csv(export_points_csv(table.points), "modbus-rtu") == table.points
    storage = RtuServerStorage(tmp_path)
    storage.save_plan(plan())
    assert storage.load_plan() == plan()
    record = project_record(Project("old"))
    record["schema_version"] = 3
    del record["servers"]["modbus-rtu"]
    assert decode_project(record).rtu == RtuServerPlan()


@pytest.mark.parametrize("unit", (0, 248, 255))
def test_reserved_station_ids_rejected(unit):
    with pytest.raises(ClientError):
        ModbusRtuTarget(unit, Area.HR, 0, 1).validate()
    with pytest.raises(ValueError):
        replace(plan(), stations=(Station(unit),)).validate()


def test_crc_and_truncated_diagnostics():
    frame = bytes.fromhex("010300000001840a")
    assert not any(field.name == "解析失败" for field in decode_packet("modbus-rtu", frame, request=True))
    assert any(field.name == "解析失败" for field in decode_packet("modbus-rtu", frame[:-1], request=True))
    assert any("CRC" in field.value for field in decode_packet("modbus-rtu", frame[:-1] + b"X", request=True))


def _adu(payload):
    return payload + FramerRTU.compute_CRC(payload).to_bytes(2, "big")


@pytest.mark.parametrize("fault", ("crc", "truncated", "station", "function", "length", "echo"))
def test_corrupt_responses_pause_station_and_preserve_other_stations(cable, fault):
    async def run():
        records = []
        server = RtuServerAdapter(plan(), ServerCallbacks(lambda time, request: records.append(request), lambda change: None))
        packets = []
        client = ModbusRtuClient(ModbusRtuConfig(timeout_ms=100), ClientEvents(lambda sending, data: packets.append((sending, data)), lambda: None))
        await server.bind()
        await client.connect()
        transport = cable[0]["COM2"]
        write = transport.write
        def corrupt(data):
            original_size = len(data)
            if fault == "crc":
                data = data[:-1] + bytes([data[-1] ^ 1])
            elif fault == "truncated":
                data = data[:3]
            elif fault == "station":
                data = _adu(bytes([2]) + data[1:-2])
            elif fault == "function":
                data = _adu(data[:1] + bytes([4]) + data[2:-2])
            elif fault == "length":
                data = _adu(bytes.fromhex("01030400010002"))
            elif fault == "echo":
                data = _adu(bytes.fromhex("010600000063"))
            write(data)
            return original_size
        transport.write = corrupt
        try:
            with pytest.raises(ClientError) as invalid:
                if fault == "echo":
                    await client.write(ModbusRtuTarget(1, Area.HR, 0, 1), (11,))
                else:
                    await client.read(ModbusRtuTarget(1, Area.HR, 0, 1))
            assert invalid.value.kind == ErrorKind.RESPONSE
            transport.write = write
            with pytest.raises(ClientError) as paused:
                await client.read(ModbusRtuTarget(1, Area.HR, 0, 1))
            assert paused.value.kind == ErrorKind.INPUT
            assert (await client.read(ModbusRtuTarget(2, Area.HR, 0, 1)))[0].value == 42
            # Each operation sends once even on corrupt replies.
            assert len([data for sending, data in packets if sending]) == 2
        finally:
            await client.close()
            await server.shutdown()
    asyncio.run(run())


def test_late_frame_cannot_satisfy_other_station_request(cable):
    async def run():
        server = RtuServerAdapter(plan(), ServerCallbacks(lambda *args: None, lambda change: None))
        client = ModbusRtuClient(ModbusRtuConfig(timeout_ms=300), ClientEvents(lambda *args: None, lambda: None))
        await server.bind()
        await client.connect()
        port = cable[0]["COM2"]
        write = port.write
        handles = []
        def delayed(data):
            delay = .36 if data[0] == 1 else .09
            handles.append(asyncio.get_running_loop().call_later(delay, write, data))
            return len(data)
        port.write = delayed
        try:
            with pytest.raises(ClientError):
                await client.read(ModbusRtuTarget(1, Area.HR, 0, 1))
            assert (await client.read(ModbusRtuTarget(2, Area.HR, 0, 1)))[0].value == 42
            with pytest.raises(ClientError) as paused:
                await client.read(ModbusRtuTarget(1, Area.HR, 0, 1))
            assert paused.value.kind == ErrorKind.INPUT
        finally:
            for handle in handles:
                handle.cancel()
            await client.close()
            await server.shutdown()
    asyncio.run(run())


def test_server_drops_bad_crc_foreign_and_broadcast_frames(cable):
    async def run():
        records = []
        server = RtuServerAdapter(plan(), ServerCallbacks(lambda time, item: records.append(item), lambda change: None))
        await server.bind()
        try:
            source = serial.serial_for_url("COM1")
            source.write(bytes.fromhex("0106000000090000"))
            async with asyncio.timeout(1):
                while not records:
                    await asyncio.sleep(.01)
            assert server.read_current(ModbusServerTarget(1, Area.HR, 0)) == 7
            assert records[-1].error == ErrorKind.RESPONSE
            for unit in (0, 3):
                source.write(_adu(bytes([unit]) + bytes.fromhex("0600000009")))
                await asyncio.sleep(.08)
            assert source.in_waiting == 0
            assert server.read_current(ModbusServerTarget(1, Area.HR, 0)) == 7
            source.close()
        finally:
            await server.shutdown()
    asyncio.run(run())


def test_cancel_open_releases_client_and_server_conflict_does_not_steal_port(cable, monkeypatch):
    async def run():
        server = RtuServerAdapter(plan(), ServerCallbacks(lambda *args: None, lambda change: None))
        await server.bind()
        conflicting = ModbusRtuClient(ModbusRtuConfig("com2"), ClientEvents(lambda *args: None, lambda: None))
        with pytest.raises(ClientError):
            await conflicting.connect()
        await conflicting.close()
        with pytest.raises(OSError):
            acquire("COM2", object())
        await server.shutdown()
        client = ModbusRtuClient(ModbusRtuConfig(), ClientEvents(lambda *args: None, lambda: None))
        opened = asyncio.Event()
        async def cancelled_open():
            opened.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(client._client, "connect", cancelled_open)
        task = asyncio.create_task(client.connect())
        await opened.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        owner = object()
        acquire("COM1", owner)
        release("COM1", owner)
    asyncio.run(run())


def test_rtu_write_verification_scene_and_report(cable, tmp_path):
    from datetime import timedelta
    from all_protocol.application.contracts import VerificationOptions, VerificationStatus
    from all_protocol.application.server_session import ServerSession
    from all_protocol.application.simulation import SimulationRule, RuleKind
    from all_protocol.application.journal import RetainedLog
    from all_protocol.reporting import ReportScope, ReportSnapshot
    from all_protocol.server_protocols import SERVER_PROTOCOLS
    async def run():
        server_events, events = [], []
        registration = next(protocol.registration for protocol in SERVER_PROTOCOLS if protocol.registration.key == "modbus-rtu")
        server = ServerSession(server_events.append, registration)
        client = Session(events.append)
        point = ServerPoint(Area.HR, 0, 1, simulation=SimulationRule(RuleKind.INCREMENT, period_ms=200, step=1, low=0, high=100))
        simulation_plan = replace(plan(), stations=(Station(1, points=(point,)),))
        config = ModbusRtuConfig(timeout_ms=500)
        await server.start(simulation_plan)
        try:
            await client.connect(ProtocolRegistration("modbus-rtu", "RTU", ModbusRtuClient, True), config, config.endpoint)
            await client.execute(Command(ModbusRtuTarget(1, Area.HR, 0, 1), (23,), VerificationOptions(0, 0, 0)))
            entries = [event for event in events if isinstance(event, JournalEntry)]
            assert entries[-1].verification.status == VerificationStatus.MATCH
            history = LocalHistory(tmp_path)
            history.append_entry(entries[-1])
            assert history.recent_entries()[0] == entries[-1]
            now = datetime.now().astimezone()
            snapshot = ReportSnapshot.capture(Project("RTU", rtu=simulation_plan),
                         ReportScope(now-timedelta(minutes=1), now), RetainedLog(), (), now-timedelta(minutes=1), now)
            summary = next(item for item in snapshot.server_plans if item.protocol == "modbus-rtu")
            assert summary.endpoint == "COM2 · 9600/8E1" and summary.scenario_points == 1
        finally:
            await client.disconnect()
            await server.stop()
    asyncio.run(run())


def test_invalid_serial_plan_reports_start_failure_without_acquiring_port():
    from all_protocol.application.server_session import ServerSession
    from all_protocol.application.server_contracts import ServerLifecycle, LifecycleAction
    from all_protocol.server_protocols import SERVER_PROTOCOLS
    async def run():
        events = []
        registration = next(item.registration for item in SERVER_PROTOCOLS if item.registration.key == "modbus-rtu")
        session = ServerSession(events.append, registration)
        await session.start(replace(plan(), serial_port=""))
        assert any(isinstance(event.payload, ServerLifecycle) and event.payload.action == LifecycleAction.START_FAILED for event in events)
        await session.stop()
    asyncio.run(run())
