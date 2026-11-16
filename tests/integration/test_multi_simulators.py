import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket
from time import monotonic

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QLineEdit, QSpinBox

from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.model import Area as MbArea, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_rtu.server_model import RtuServerPlan
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.adapters.s7.model import S7Config, S7Target, Area, DataType
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.opcua.server_model import UaServerPlan, UaServerPoint, SIMULATOR_NAMESPACE
from all_protocol.adapters.opcua.model import UaTarget, UaType
from all_protocol.application.contracts import ClientEvents, ProtocolRegistration
from all_protocol.application.server_contracts import ListenerState
from all_protocol.bit_definitions import BitDefinition
from all_protocol.project import Project, ProjectFile, ProjectSimulator, ProjectWorkspace, project_record, decode_project, project_simulators
from all_protocol.reporting import ReportScope, ReportSnapshot
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.s7_form import S7Form
from all_protocol.ui.role_window import RoleWindow


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_for(condition, timeout=6):
    deadline = monotonic() + timeout
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


@pytest.fixture
def desktop(tmp_path, qt_application):
    protocol = DesktopProtocol(ProtocolRegistration("s7", "S7", S7Client, True), S7Form)
    home = RoleWindow((protocol,), tmp_path / "local")
    home.workspace = ProjectWorkspace(Project("多设备联调"))
    home._open_workspace()
    window = home.project_window
    qt_application.processEvents()
    try:
        yield qt_application, home, window
    finally:
        home.join()
        qt_application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        home.workspace = None
        home.close()
        home.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_multiple_protocol_plans_round_trip_and_empty_set(tmp_path):
    bits = (BitDefinition(0, "运行", "停止", "运行"),)
    s7 = S7ServerPlan(points=(S7ServerPoint(S7Target(Area.DB, 1, 0, DataType.WORD), 31, bit_definitions=bits),))
    mb = ServerPlan(stations=(Station(1, "一号", (ServerPoint(MbArea.HR, 0, 7, bit_definitions=bits),)), Station(2, "二号")))
    ua = UaServerPlan(points=(UaServerPoint(UaTarget(SIMULATOR_NAMESPACE, "s", "status", UaType.UInt16), 9, bit_definitions=bits),))
    plans = (("s7", s7), ("s7", replace(s7, port=1103)), ("modbus-tcp", mb),
             ("modbus-rtu", RtuServerPlan(stations=mb.stations)), ("opcua", ua))
    project = Project("联调", simulators=tuple(ProjectSimulator(str(i), f"设备{i}", protocol, plan)
                                             for i, (protocol, plan) in enumerate(plans)))
    assert project_record(project)["schema_version"] == 8
    assert decode_project(project_record(project)) == project
    file = ProjectFile()
    path = tmp_path / "multi.allprotocol"
    file.save(project, path)
    assert ProjectFile().open(path) == project
    empty = replace(project, simulators=())
    file.save(empty, path)
    assert ProjectFile().open(path).simulators == ()
    assert len(project_simulators(Project("旧项目"))) == 4


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(simulators={}),
    lambda raw: raw["simulators"].append(raw["simulators"][0]),
    lambda raw: raw["simulators"][0].update(name=" "),
    lambda raw: raw["simulators"][0].update(protocol="unknown"),
    lambda raw: raw["simulators"][0]["plan"].update(port=0),
    lambda raw: raw.update(schema_version=7),
])
def test_invalid_simulators_rejected(mutation):
    raw = project_record(Project("p", simulators=(ProjectSimulator("one", "设备", "s7", S7ServerPlan()),)))
    mutation(raw)
    with pytest.raises(ValueError):
        decode_project(raw)


def test_adding_cloning_renaming_saves_unsaved_plans_and_survives_reopen(desktop, tmp_path):
    app, home, window = desktop
    base = window._panes["s7"][2]
    base.port.setValue(12102)
    item = ProjectSimulator("second", "二号 PLC", "s7", S7ServerPlan(port=12103))
    new = window.servers.add_simulator(item)
    window._sync_servers()
    window._select(("server", item.id))
    assert new.port.value() == 12103
    assert window._items["server", item.id].text(0).endswith("二号 PLC")
    assert window.logs.currentWidget() is new.journal_panel
    assert home.workspace.project.simulators[1].plan.port == 12102
    new.port.setValue(12104)
    assert new._save_plan()
    assert next(x for x in home.workspace.project.simulators if x.id == item.id).plan.port == 12104
    window.servers.rename_simulator(item.id, "产线二号 PLC")
    saved = home._snapshot()
    path = tmp_path / "saved.allprotocol"
    ProjectFile().save(saved, path)
    reopened = ProjectFile().open(path)
    assert next(x for x in reopened.simulators if x.id == item.id).name == "产线二号 PLC"
    assert next(x for x in reopened.simulators if x.id == "s7").plan.port == 12102
    assert all(pane.state == ListenerState.STOPPED for _, pane in window.servers.panes())


def test_add_dialog_conflict_cancel_copy_and_client_entry(desktop):
    app, home, window = desktop
    base = window._panes["s7"][2]
    base._points = (replace(base._points[0], name="运行", bit_definitions=(BitDefinition(0, "运行"),)),)
    window._select(("server", "s7"))
    count = window.servers.tabs.count()
    checks = []
    def edit():
        dialog = app.activeModalWidget()
        combo = dialog.findChild(QComboBox)
        combo.setCurrentIndex(combo.findData("s7"))
        edits = dialog.findChildren(QLineEdit)
        name = next(x for x in edits if x.accessibleName() == "模拟设备名称")
        name.setText("新 PLC")
        port = dialog.findChild(QSpinBox)
        assert port.value() != base.port.value()
        port.setValue(base.port.value())
        dialog.accept()
        checks.append(dialog.error.text())
        assert "冲突" in dialog.error.text()
        port.setValue(base.port.value() + 10)
        dialog.accept()
    QTimer.singleShot(0, edit)
    window.add_simulator.click()
    assert checks and window.servers.tabs.count() == count + 1
    added = window._selection()[1]
    assert window._panes[added][2]._points[0].bit_definitions == ()
    window._select(("server", "s7"))
    def clone():
        dialog = app.activeModalWidget()
        assert not dialog.findChild(QComboBox).isEnabled()
        dialog.accept()
    QTimer.singleShot(0, clone)
    window.copy_simulator.click()
    clone_key = window._selection()[1]
    assert window._panes[clone_key][2]._points == base._points
    QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
    window.add_simulator.click()
    assert window.servers.tabs.count() == count + 2
    window.add_client.click()
    assert not window._on_server and window.client._active_device_id is None
    assert window.pages.currentWidget() is window.client


def test_two_s7_listeners_have_isolated_memory_logs_and_independent_shutdown(desktop, monkeypatch):
    app, home, window = desktop
    first_port, second_port = free_ports(2)
    first = window._panes["s7"][2]
    first.port.setValue(first_port)
    first._points = (replace(first._points[0], initial=11),)
    second = window.servers.add_simulator(ProjectSimulator("second", "二号", "s7",
        S7ServerPlan(port=second_port, points=(replace(first._points[0], initial=22),))))
    first.listen.click()
    second.listen.click()
    wait_for(lambda: first.state == second.state == ListenerState.LISTENING)
    target = first._points[0].target
    async def read_write():
        clients = [S7Client(S7Config("127.0.0.1", port), ClientEvents(lambda *_: None, lambda: None))
                   for port in (first_port, second_port)]
        try:
            await asyncio.gather(*(client.connect() for client in clients))
            assert [(await client.read(target))[0].value for client in clients] == [11, 22]
            await clients[0].write(target, (77,))
            assert [(await client.read(target))[0].value for client in clients] == [77, 22]
        finally:
            await asyncio.gather(*(client.close() for client in clients))
    asyncio.run(read_write())
    wait_for(lambda: bool(first.report_log().entries) and bool(second.report_log().entries))
    assert all(x.endpoint.endswith(str(first_port)) and x.protocol == "s7" for x in first.report_log().entries)
    assert all(x.endpoint.endswith(str(second_port)) and x.protocol == "s7" for x in second.report_log().entries)
    with pytest.raises(ValueError, match="先停止"):
        window.servers.remove_simulator("second")
    second.listen.click()
    wait_for(lambda: second.state == ListenerState.STOPPED)
    window.servers.remove_simulator("second")
    app.processEvents()
    assert first.state == ListenerState.LISTENING
    assert "second" not in window._panes
    first.listen.click()
    wait_for(lambda: first.state == ListenerState.STOPPED)
    # Removed bridges must not make later application shutdown wait forever.
    window.servers.shutdown_embedded()
    wait_for(lambda: len(window.servers._stopped) == len(window.servers._bridges))


def test_remove_all_defaults_stays_empty_after_save_and_can_add_again(desktop, tmp_path):
    app, home, window = desktop
    for key in list(window._panes):
        window.servers.remove_simulator(key)
    app.processEvents()
    assert not window._panes and not window._on_server
    path = tmp_path / "empty.allprotocol"
    ProjectFile().save(home._snapshot(), path)
    assert project_simulators(ProjectFile().open(path)) == ()
    window.servers.add_simulator(ProjectSimulator("new", "PLC", "s7", S7ServerPlan()))
    app.processEvents()
    assert list(window._panes) == ["new"]


def test_report_includes_each_simulated_device(desktop):
    app, home, window = desktop
    window.servers.add_simulator(ProjectSimulator("second", "二号", "s7", S7ServerPlan(port=1103)))
    now = datetime.now(timezone.utc)
    snapshot = window._capture_report(ReportScope(now-timedelta(minutes=1), now), now)
    s7 = [item for item in snapshot.server_plans if item.protocol == "s7"]
    assert {item.id for item in s7} == {"s7", "second"}
    assert {item.name for item in s7} == {"Siemens S7", "二号"}
    assert len(snapshot.server_logs) == 5


def test_multiple_modbus_tcp_services_and_unit_ids(desktop):
    app, home, window = desktop
    ports = free_ports(2)
    stations = lambda value: (Station(1, "一号", (ServerPoint(MbArea.HR, 0, value),)),
                              Station(2, "二号", (ServerPoint(MbArea.HR, 0, value + 1),)))
    panes = [window.servers.add_simulator(ProjectSimulator(f"mb{i}", f"网关{i}", "modbus-tcp",
             ServerPlan(port=port, stations=stations(value)))) for i, (port, value) in enumerate(zip(ports, (10, 20)))]
    for pane in panes:
        pane.listen.click()
    wait_for(lambda: all(pane.state == ListenerState.LISTENING for pane in panes), timeout=15)
    async def check():
        clients = [ModbusTcpClient(ModbusTcpConfig("127.0.0.1", port), ClientEvents(lambda *_: None, lambda: None)) for port in ports]
        try:
            await asyncio.gather(*(client.connect() for client in clients))
            target = ModbusTarget(1, MbArea.HR, 0, 1)
            assert [(await client.read(target))[0].value for client in clients] == [10, 20]
            assert [(await client.read(replace(target, unit_id=2)))[0].value for client in clients] == [11, 21]
            await clients[0].write(target, (99,))
            assert [(await client.read(target))[0].value for client in clients] == [99, 20]
        finally:
            await asyncio.gather(*(client.close() for client in clients))
    asyncio.run(check())


def test_two_opcua_services_have_distinct_identity_and_node_values(desktop):
    from asyncua import Client
    app, home, window = desktop
    ports = free_ports(2)
    target = UaTarget(SIMULATOR_NAMESPACE, "s", "counter", UaType.Int32)
    panes = [window.servers.add_simulator(ProjectSimulator(f"ua{i}", f"UA{i}", "opcua",
             UaServerPlan(port=port, points=(UaServerPoint(target, value),), security="None/None")))
             for i, (port, value) in enumerate(zip(ports, (10, 20)))]
    for pane in panes:
        pane.listen.click()
    wait_for(lambda: all(pane.state == ListenerState.LISTENING for pane in panes), timeout=15)
    async def check():
        clients = [Client(f"opc.tcp://127.0.0.1:{port}/allprotocol/") for port in ports]
        try:
            await asyncio.gather(*(client.connect() for client in clients))
            nodes = [client.get_node(target.node_id(await client.get_namespace_index(SIMULATOR_NAMESPACE))) for client in clients]
            identities = [await client.get_node("i=2254").read_value() for client in clients]
            assert identities[0] != identities[1]
            assert [await node.read_value() for node in nodes] == [10, 20]
            from asyncua import ua
            await nodes[0].write_value(77, ua.VariantType.Int32)
            assert [await node.read_value() for node in nodes] == [77, 20]
        finally:
            await asyncio.gather(*(client.disconnect() for client in clients))
    asyncio.run(check())


def test_rtu_dialog_prevents_duplicate_bus_and_offers_new_station(desktop):
    app, home, window = desktop
    window._select(("server", "modbus-rtu"))
    assert not window.add_bus_station.isHidden()
    original_count = window.servers.tabs.count()
    checks = []
    def edit():
        dialog = app.activeModalWidget()
        combo = dialog.findChild(QComboBox)
        combo.setCurrentIndex(combo.findData("modbus-rtu"))
        host = next(x for x in dialog.findChildren(QLineEdit) if x.accessibleName() == "模拟设备监听地址或串口")
        host.setText("com2")
        dialog.accept()
        checks.append(dialog.error.text())
        assert "新增从站" in dialog.error.text()
        host.setText("COM9")
        dialog.accept()
    QTimer.singleShot(0, edit)
    window.add_simulator.click()
    assert checks and window.servers.tabs.count() == original_count + 1
    assert window._panes[window._selection()[1]][2]._plan().serial_port == "COM9"


def test_failed_listener_does_not_stop_other_device(desktop):
    app, home, window = desktop
    port = free_ports(1)[0]
    base = window._panes["s7"][2]
    base.port.setValue(port)
    second = window.servers.add_simulator(ProjectSimulator("collision", "冲突设备", "s7", S7ServerPlan(port=port)))
    base.listen.click()
    wait_for(lambda: base.state == ListenerState.LISTENING)
    second.listen.click()
    wait_for(lambda: second.state == ListenerState.STOPPED and bool(second.listener_error))
    assert base.state == ListenerState.LISTENING
    async def check():
        client = S7Client(S7Config("127.0.0.1", port), ClientEvents(lambda *_: None, lambda: None))
        try:
            await client.connect()
            assert (await client.read(base._points[0].target))[0].value == 0
        finally:
            await client.close()
    asyncio.run(check())


def test_saved_instances_restore_tree_selection_and_stay_stopped(desktop, tmp_path):
    app, home, window = desktop
    item = ProjectSimulator("two", "二号 PLC", "s7", S7ServerPlan(port=1103))
    window.servers.add_simulator(item)
    window._sync_servers()
    window._select(("server", item.id))
    project = home._snapshot()
    path = tmp_path / "reopen.allprotocol"
    home.workspace.file.save(project, path)
    window._confirm_close = lambda _: True
    window.request_close()
    wait_for(lambda: home.project_window is None)
    file = ProjectFile()
    home.workspace = ProjectWorkspace(file.open(path), file)
    home._open_workspace()
    restored = home.project_window
    app.processEvents()
    assert restored._selection() == ("server", item.id)
    assert restored._panes[item.id][1] == item.name
    assert len(restored._panes) == 5
    assert all(pane.state == ListenerState.STOPPED for _, pane in restored.servers.panes())
    assert restored.logs.currentWidget() is restored._panes[item.id][2].journal_panel
