"""Interop with the separate synchronous python-opcua server stack."""

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import socket
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from opcua import Server, ua as server_ua
from opcua import Client as IndependentClient
from asyncua import ua
import pytest

from all_protocol.adapters.opcua.client import OpcUaClient, discover_endpoints
from all_protocol.adapters.opcua.identity import IdentityStore, UaIdentity
from all_protocol.adapters.opcua.model import OpcUaConfig, UaBrowseTarget, UaTarget, UaType
from all_protocol.adapters.opcua.server_model import UaServerPlan, UaServerPoint, SIMULATOR_NAMESPACE
from all_protocol.adapters.opcua.server_adapter import UaServerAdapter
from all_protocol.application.contracts import (
    ClientError, ClientEvents, Command, ErrorKind, JournalEntry, ProtocolRegistration,
    ReadUpdate, TableReadUpdate, VerificationOptions, VerificationStatus, WriteStatus,
)
from all_protocol.application.packet_decoder import decode_packet
from all_protocol.application.sampling import Quality, SampleStore
from all_protocol.application.session import Session
from all_protocol.application.server_session import ServerSession
from all_protocol.application.server_contracts import ServerCallbacks, ServerRegistration, ServerCapability, ListenerState, RequestMetadata
from all_protocol.application.simulation import SimulationCommand, SimulationRule, RuleKind
from all_protocol.point_tables import (
    PointDefinition, PointTable, export_points_csv, export_table_json, import_points_csv, import_table_json, target_record,
)
from all_protocol.project import Project, ProjectFile, ProjectWorkspace, decode_project, project_record
from all_protocol.storage import ConnectionProfile, LocalHistory, UaServerStorage
from all_protocol.reporting import ReportSnapshot, ReportScope, ReportCancellation, prepare_report
from all_protocol.application.journal import RetainedLog
from all_protocol.application.diagnostics import DiagnosticRow, request_stats


URI = "urn:allprotocol:interop"
TARGET = UaTarget(URI, "s", "Counter", UaType.Int32)
pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow\\(\\) is deprecated:DeprecationWarning:opcua.*")


def server_certificate(directory, *, expired=False, hostname="127.0.0.1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OPC UA interop test")])
    now = datetime.now(timezone.utc)
    address = x509.IPAddress(ipaddress.ip_address(hostname))
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 2))
            .add_extension(x509.SubjectAlternativeName([address, x509.UniformResourceIdentifier(URI)]), False)
            .add_extension(x509.KeyUsage(True, True, True, True, False, False, False, None, None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(key, hashes.SHA256()))
    certpath, keypath = directory / "server.der", directory / "server.pem"
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return certpath, keypath


@pytest.fixture
def bench(tmp_path, request):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"opc.tcp://127.0.0.1:{port}/interop/"
    certpath, keypath = server_certificate(tmp_path, **getattr(request, "param", {}))
    server = Server()
    server.set_endpoint(url)
    server.set_application_uri(URI)
    server.set_security_policy([server_ua.SecurityPolicyType.NoSecurity,
                                server_ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
    server.set_security_IDs(["Anonymous"])
    server.load_certificate(str(certpath))
    server.load_private_key(str(keypath))
    index = server.register_namespace(URI)
    obj = server.nodes.objects.add_object(server_ua.NodeId("Demo", index), "Demo")
    counter = obj.add_variable(server_ua.NodeId("Counter", index), "Counter", server_ua.Variant(42, server_ua.VariantType.Int32))
    counter.set_writable()
    obj.add_variable(server_ua.NodeId("ReadOnly", index), "ReadOnly", server_ua.Variant(1, server_ua.VariantType.Int32))
    text = obj.add_variable(server_ua.NodeId("Text", index), "Text", "hello")
    text.set_writable()
    obj.add_variable(server_ua.NodeId("Array", index), "Array", [1, 2, 3])
    store = IdentityStore(tmp_path / "identity")
    cert, key, app_uri = store.generate()
    identity_id = store.save(UaIdentity(cert, key, str(certpath), app_uri))
    server.start()
    try:
        yield server, counter, url, store, identity_id
    finally:
        server.stop()


def client_for(bench, *, secure=False, events=None):
    _, _, url, store, identity_id = bench
    return OpcUaClient(OpcUaConfig(url, "Basic256Sha256/SignAndEncrypt" if secure else "None/None",
                                  identity_id if secure else "", 5000),
                       events or ClientEvents(lambda *_: None, lambda: None), store)


@pytest.mark.parametrize("secure", (False, True), ids=("anonymous", "encrypted"))
def test_independent_discovery_browse_read_write_and_packets(bench, secure):
    async def run():
        endpoints = await discover_endpoints(bench[2], 5000)
        assert any(item.policy == "Basic256Sha256" and item.mode == "SignAndEncrypt" and item.fingerprint for item in endpoints)
        packets = []
        client = client_for(bench, secure=secure, events=ClientEvents(lambda tx, data: packets.append((tx, data)), lambda: None))
        try:
            await client.connect()
            points = await client.read(UaBrowseTarget(UaTarget(URI, "s", "Demo")))
            refs = {point.reference.name: point.reference for point in points}
            assert refs["Counter"].data_type == "Int32" and refs["Counter"].writable
            assert not refs["ReadOnly"].writable and not refs["Array"].data_type
            point = (await client.read(TARGET))[0]
            assert point.value == 42 and point.metadata.quality == "Good" and point.metadata.source_timestamp
            await client.write(TARGET, (123,))
            assert (await client.read(TARGET))[0].value == 123
            assert bench[1].get_value() == 123
            for value in ("spaces, commas\nand 中文", ""):
                text = replace(TARGET, identifier="Text", data_type=UaType.String)
                await client.write(text, (value,))
                assert (await client.read(text))[0].value == value
            assert any(tx for tx, _ in packets) and any(not tx for tx, _ in packets)
            assert any(data.startswith(b"MSG") for _, data in packets)
        finally:
            await client.close()
            await client.close()
    asyncio.run(run())


def test_native_bad_uncertain_quality_preserves_last_good(bench, tmp_path):
    async def run():
        events = []
        session = Session(events.append)
        registration = ProtocolRegistration("opcua", "UA", lambda config, events: OpcUaClient(config, events, bench[3]), True)
        await session.connect(registration, OpcUaConfig(bench[2], "None/None", timeout_ms=5000), bench[2])
        table = PointTable("table", "device", "values", (PointDefinition("point", "counter", TARGET),))
        samples = SampleStore()
        samples.sync((table,))
        samples.set_generation(session._state.generation)
        try:
            await session.execute_table(table.id, (("point", Command(TARGET)),))
            samples.update(next(event for event in reversed(events) if isinstance(event, TableReadUpdate)))
            previous = samples.samples[(table.id, "point")]
            assert previous.raw_value == 42 and previous.quality == Quality.GOOD
            for code, quality in ((server_ua.StatusCodes.Uncertain, Quality.UNCERTAIN),
                                  (server_ua.StatusCodes.BadSensorFailure, Quality.BAD)):
                value = server_ua.DataValue(server_ua.Variant(999, server_ua.VariantType.Int32))
                value.StatusCode = server_ua.StatusCode(code)
                bench[1].set_data_value(value)
                await session.execute_table(table.id, (("point", Command(TARGET)),))
                event = next(event for event in reversed(events) if isinstance(event, TableReadUpdate))
                samples.update(event)
                current = samples.samples[(table.id, "point")]
                assert current.raw_value == 42 and current.quality == quality
                assert current.last_success_at == previous.last_success_at
                assert current.metadata.status_code == code and "0x" in current.detail
                history = LocalHistory(tmp_path / "history")
                history.append_entry(event.update.entry)
                assert history.recent_entries()[0] == event.update.entry
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_rejections_and_type_mismatches(bench):
    async def run():
        client = client_for(bench)
        try:
            await client.connect()
            for target, code in ((replace(TARGET, identifier="missing"), ua.StatusCodes.BadNodeIdUnknown),):
                point = (await client.read(target))[0]
                assert point.metadata.quality == "Bad" and point.metadata.status_code == code
            for target in (replace(TARGET, namespace_uri="urn:missing"), replace(TARGET, data_type=UaType.Double),
                           replace(TARGET, identifier="Array")):
                with pytest.raises(ClientError):
                    await client.read(target)
            for target in (replace(TARGET, identifier="ReadOnly"), replace(TARGET, data_type=UaType.Double)):
                with pytest.raises(ClientError) as rejected:
                    await client.write(target, (1,))
                assert rejected.value.write_sent is False
            assert bench[1].get_value() == 42
        finally:
            await client.close()
    asyncio.run(run())


def test_namespace_uri_is_resolved_again_on_new_session(bench):
    async def run():
        client = client_for(bench)
        await client.connect()
        try:
            index = client._namespaces.index(URI)
            assert client._node(TARGET).nodeid.NamespaceIndex == index
            # Address resolution uses the current NamespaceArray, never saved ns=2.
            client._namespaces = ("reserved", "another", "extra", URI)
            assert client._node(TARGET).nodeid.NamespaceIndex == 3
        finally:
            await client.close()
        await client.connect()
        try:
            assert (await client.read(TARGET))[0].value == 42
        finally:
            await client.close()
    asyncio.run(run())


def test_secure_missing_identity_and_wrong_trust_are_rejected(bench, tmp_path):
    async def run():
        client = client_for(bench, secure=True)
        client.config = replace(client.config, identity_id="missing_identity")
        with pytest.raises(ClientError, match="本机缺少"):
            await client.connect()
        cert, key, uri = bench[3].generate()
        wrong_id = bench[3].save(UaIdentity(cert, key, cert, uri))
        client = client_for(bench, secure=True)
        client.config = replace(client.config, identity_id=wrong_id)
        with pytest.raises(ClientError):
            await client.connect()
        assert not client._online
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("bench", ({"expired": True}, {"hostname": "192.0.2.1"}), indirect=True, ids=("expired", "hostname"))
def test_secure_certificate_validation_rejects_expiry_and_host(bench):
    async def run():
        client = client_for(bench, secure=True)
        with pytest.raises(ClientError):
            await client.connect()
        assert not client._online
        await client.close()
    asyncio.run(run())


def test_cancelled_handshake_closes_tcp_socket():
    async def run():
        accepted, lost = asyncio.Event(), asyncio.Event()

        async def peer(reader, writer):
            accepted.set()
            try:
                await reader.read(1024)  # HEL is deliberately left unanswered.
                await reader.read()
                lost.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        url = f"opc.tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        client = OpcUaClient(OpcUaConfig(url, "None/None"), ClientEvents(lambda *_: None, lambda: None))
        task = asyncio.create_task(client.connect())
        try:
            await accepted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(lost.wait(), 3)
            assert not client._online
        finally:
            await client.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_session_write_evidence_and_roundtrip(bench, tmp_path):
    async def run():
        events = []
        session = Session(events.append)
        registration = ProtocolRegistration("opcua", "UA", lambda config, events: OpcUaClient(config, events, bench[3]), True)
        await session.connect(registration, OpcUaConfig(bench[2], "None/None", timeout_ms=5000), bench[2])
        try:
            await session.execute(Command(TARGET, (77,), VerificationOptions(0)))
            journals = [event for event in events if isinstance(event, JournalEntry)]
            write, read = journals[-2:]
            assert write.write.status == WriteStatus.ACKNOWLEDGED and read.verification.status == VerificationStatus.MATCH
            await session.execute(Command(replace(TARGET, identifier="ReadOnly"), (4,)))
            rejected = next(event for event in reversed(events) if isinstance(event, JournalEntry))
            assert rejected.write.status == WriteStatus.NOT_SENT
            history = LocalHistory(tmp_path / "history")
            for entry in (write, read, rejected):
                history.append_entry(entry)
            assert history.recent_entries() == (rejected, read, write)
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_timed_out_write_is_unknown_without_replay(bench, monkeypatch):
    service = bench[0].iserver.attribute_service
    original = service.write
    calls = []

    def delayed(parameters, *args, **kwargs):
        calls.append(parameters)
        result = original(parameters, *args, **kwargs)
        time.sleep(.5)
        return result

    monkeypatch.setattr(service, "write", delayed)

    async def run():
        events = []
        session = Session(events.append)
        registration = ProtocolRegistration("opcua", "UA", lambda config, events: OpcUaClient(config, events, bench[3]), True)
        config = OpcUaConfig(bench[2], "None/None", timeout_ms=250)
        await session.connect(registration, config, config.endpoint)
        try:
            await session.execute(Command(TARGET, (88,), VerificationOptions(0)))
            entry = next(event for event in reversed(events) if isinstance(event, JournalEntry))
            assert entry.write.status == WriteStatus.UNKNOWN and entry.error == ErrorKind.TIMEOUT
            assert len(calls) == 1 and bench[1].get_value() == 88
            assert not any(isinstance(event, JournalEntry) and event.verification for event in events)
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_bad_quality_readback_is_unavailable(bench):
    events = []

    def emit(event):
        events.append(event)
        if isinstance(event, JournalEntry) and event.write and event.write.status == WriteStatus.ACKNOWLEDGED:
            value = server_ua.DataValue(server_ua.Variant(77, server_ua.VariantType.Int32))
            value.StatusCode = server_ua.StatusCode(server_ua.StatusCodes.BadSensorFailure)
            bench[1].set_data_value(value)

    async def run():
        session = Session(emit)
        registration = ProtocolRegistration("opcua", "UA", lambda config, events: OpcUaClient(config, events, bench[3]), True)
        await session.connect(registration, OpcUaConfig(bench[2], "None/None", timeout_ms=5000), bench[2])
        try:
            await session.execute(Command(TARGET, (77,), VerificationOptions(0)))
            entry = next(event for event in reversed(events) if isinstance(event, JournalEntry))
            assert entry.verification.status == VerificationStatus.UNAVAILABLE
            assert entry.value_metadata[0].quality == "Bad"
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_point_table_csv_json_and_project_v5(tmp_path):
    point = PointDefinition("point", "counter", TARGET)
    table = PointTable("table", "device", "test", (point,))
    assert import_points_csv(export_points_csv((point,)), "opcua") == (point,)
    restored = import_table_json(export_table_json(table, "opcua"), "opcua", "new-device")
    assert restored.points[0].target == TARGET and restored.device_id == "new-device"
    config = OpcUaConfig("opc.tcp://127.0.0.1:4840/test/", identity_id="ua_local_identity")
    profile = ConnectionProfile("opcua", config.endpoint, {**asdict(config), **target_record(TARGET)}, 1000)
    project = Project("UA project").remember(profile)
    table = replace(table, device_id=project.devices[0].id)
    project = project.with_table(table)
    record = project_record(project)
    assert record["schema_version"] == 5 and decode_project(record) == project
    assert not any(word in json.dumps(record) for word in ("private_key", "trusted_server", ".pem", ".der"))
    file = ProjectFile()
    path = tmp_path / "ua.allprotocol"
    file.save(project, path)
    assert file.open(path) == project
    record["devices"][0]["profile"]["fields"]["private_key"] = "forbidden.pem"
    with pytest.raises(ValueError, match="凭据"):
        decode_project(record)


@pytest.mark.parametrize("url", ("http://host:4840/", "opc.tcp://user:password@host:4840", "opc.tcp://host", "opc.tcp://host:4840/?password=a"))
def test_config_rejects_credentials_and_invalid_endpoints(url):
    with pytest.raises(ClientError):
        OpcUaConfig(url).validate()


@pytest.mark.parametrize("target", (replace(TARGET, identifier_type="i", identifier="-1"),
                                   replace(TARGET, identifier_type="g", identifier="bad"),
                                   replace(TARGET, identifier_type="b", identifier="xyz"),
                                   replace(TARGET, count=True), replace(TARGET, namespace_uri="")))
def test_invalid_nodes(target):
    with pytest.raises(ClientError):
        target.validate()


def test_ua_tcp_packet_decoder_handles_partial_and_multiple_chunks():
    data = b"MSGF" + (16).to_bytes(4, "little") + (7).to_bytes(4, "little") + b"test"
    decoded = decode_packet("opcua", data * 2, request=True)
    assert len(decoded) == 2 and decoded[0].children[3].value == "7"
    assert decode_packet("opcua", data[:-1], request=True)[-1].name == "解析失败"


async def until(condition, timeout=3):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(.01)


def test_subscription_notifications_stop_reconnect_and_overflow(bench):
    from all_protocol.application.recovery import RecoveryPolicy
    from all_protocol.application.contracts import SubscriptionHealth
    async def run():
        events = []
        session = Session(events.append)
        registration = ProtocolRegistration("opcua", "UA", lambda config, events: OpcUaClient(config, events, bench[3]), True)
        config = OpcUaConfig(bench[2], "None/None", timeout_ms=5000)
        commands = (("point", Command(TARGET)),)
        await session.connect(registration, config, config.endpoint)
        try:
            assert await session.start_table_subscription("table", commands, 200, 2)
            await until(lambda: any(isinstance(event, TableReadUpdate) for event in events))
            bench[1].set_value(server_ua.Variant(66, server_ua.VariantType.Int32))
            await until(lambda: any(isinstance(event, TableReadUpdate) and event.update.points
                                    and event.update.points[0].value == 66 for event in events))
            assert "采样" in next(event for event in reversed(events) if isinstance(event, JournalEntry)).detail
            notification_rows = [DiagnosticRow.from_client(event) for event in events
                                 if isinstance(event, JournalEntry) and event.function == "DataChangeNotification"]
            assert notification_rows and request_stats(notification_rows).count == 0
            assert all(row.elapsed_ms is None for row in notification_rows)
            old = session._subscriptions["table"][1]
            # Queue overflow is reported through the StatusCode information bit.
            value = ua.DataValue(ua.Variant(67, ua.VariantType.Int32), ua.StatusCode(0x480))
            result = ua.PublishResult()
            notification = ua.DataChangeNotification()
            notification.MonitoredItems = [ua.MonitoredItemNotification(ClientHandle=1, Value=value)]
            result.NotificationMessage.NotificationData = [notification]
            await old.publish(result)
            assert "队列溢出" in next(event for event in reversed(events) if isinstance(event, JournalEntry)).detail
            health = next(event for event in reversed(events) if isinstance(event, SubscriptionHealth))
            assert health.alive and health.last_publish_at and health.last_notification_at
            value_count = len([event for event in events if isinstance(event, TableReadUpdate)])
            await old.publish(ua.PublishResult())
            keepalive = next(event for event in reversed(events) if isinstance(event, SubscriptionHealth))
            assert keepalive.last_notification_at == health.last_notification_at
            assert keepalive.last_publish_at >= health.last_publish_at
            assert len([event for event in events if isinstance(event, TableReadUpdate)]) == value_count
            await session.stop_table_subscription("table")
            count = len(events)
            await old.publish(result)
            assert len(events) == count and not session._state.subscription_groups
            assert await session.start_table_subscription("table", commands, 200)
            old_generation = session._generation
            old = session._subscriptions["table"][1]
            await session.disconnect()
            await session.connect(registration, config, config.endpoint)
            count = len(events)
            await old.publish(result)
            assert len(events) == count and session._generation > old_generation
            await session.disconnect()
            policy = RecoveryPolicy(restore_reads=True)
            await session.connect(registration, config, config.endpoint, policy)
            assert await session.start_table_subscription("table", commands, 200)
            await session.disconnect(unexpected=True)
            await session.connect(registration, config, config.endpoint, policy)
            assert session._state.subscription_groups == ("table",)
            assert session._subscriptions["table"][1] is not old
            await session.start_table_polling("table", commands, 200)
            assert not session._state.subscription_groups and session._state.polling_groups == ("table",)
            await session.stop_all_polling()
            started, release = asyncio.Event(), asyncio.Event()

            async def late_failure(*args):
                started.set()
                await release.wait()
                raise ClientError(ErrorKind.DISCONNECTED, "old subscription setup failed")

            session._client.subscribe = late_failure
            pending = asyncio.create_task(session.start_table_subscription("table", commands, 200))
            await started.wait()
            await session.disconnect()
            await session.connect(registration, config, config.endpoint)
            count = len(events)
            release.set()
            assert await pending is False
            assert len(events) == count  # A retired setup failure cannot alter the new session.
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_simulator_independent_client_scenarios_and_subscriptions(tmp_path):
    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        target = UaTarget(SIMULATOR_NAMESPACE, "s", "Counter", UaType.Int32)
        point = UaServerPoint(target, 2, "Counter", True,
                              SimulationRule(RuleKind.INCREMENT, period_ms=200, low=2, high=10, step=1))
        plan = UaServerPlan(port=port, points=(point,), security="None/None")
        registration = ServerRegistration("opcua", "UA", UaServerAdapter, frozenset(ServerCapability),
                                           lambda plan: plan.simulation_points(configured_only=True))
        events = []
        server = ServerSession(events.append, registration)
        await server.start(plan)
        assert server._state == ListenerState.LISTENING, events
        independent = IndependentClient(plan.endpoint)
        notifications = []
        client = OpcUaClient(OpcUaConfig(plan.endpoint, "None/None", timeout_ms=5000),
                             ClientEvents(lambda *_: None, lambda: None))
        try:
            await asyncio.to_thread(independent.connect)
            index = await asyncio.to_thread(independent.get_namespace_index, SIMULATOR_NAMESPACE)
            node = independent.get_node(server_ua.NodeId("Counter", index))
            assert await asyncio.to_thread(node.get_value) == 2
            await asyncio.to_thread(node.set_value, server_ua.Variant(5, server_ua.VariantType.Int32))
            assert await server.read_current(target) == 5
            await client.connect()
            subscription = await client.subscribe((target,), 200, 10,
                                                   lambda index, point, detail: notifications.append((point, detail)))
            await until(lambda: notifications)
            await server.write_current(target, 7)
            await until(lambda: any(point and point.value == 7 for point, _ in notifications))
            await server.control_simulation(SimulationCommand.RELEASE, target)
            await server.control_simulation(SimulationCommand.START)
            await until(lambda: any(point and point.value not in (5, 7) for point, _ in notifications))
            assert any(isinstance(event.payload, RequestMetadata) and dict(event.payload.details).get("service") == "Write"
                       for event in events)
            await subscription.close()
            storage = UaServerStorage(tmp_path / "local")
            storage.save_plan(plan)
            assert storage.load_plan() == plan
            project = Project("UA simulator", ua=plan)
            file = ProjectFile()
            file.save(project, tmp_path / "simulator.allprotocol")
            assert file.open(tmp_path / "simulator.allprotocol") == project
            for event in events:
                if isinstance(event.payload, RequestMetadata):
                    storage.append_event(event)
            now = datetime.now().astimezone()
            snapshot = ReportSnapshot.capture(project, ReportScope(now - timedelta(minutes=1), now),
                RetainedLog(), (("opcua", storage.read_log()),), now - timedelta(minutes=1), now)
            report = json.loads(prepare_report(snapshot, ReportCancellation()).json)
            assert any(item["protocol"] == "opcua" for item in report["records"])
        finally:
            await client.close()
            await asyncio.to_thread(independent.disconnect)
            await server.stop()
            await server.stop()
        assert server._state == ListenerState.STOPPED
    asyncio.run(run())


def test_simulator_requires_explicit_mode_loopback_and_unique_addresses():
    target = UaTarget(SIMULATOR_NAMESPACE, "s", "Counter", UaType.Int32)
    point = UaServerPoint(target)
    for plan in (UaServerPlan(points=(point,)), UaServerPlan(host="0.0.0.0", points=(point,), security="None/None"),
                 UaServerPlan(points=(point, replace(point, target=replace(target, data_type=UaType.Double))), security="None/None")):
        with pytest.raises(ValueError):
            plan.validate(starting=True)
