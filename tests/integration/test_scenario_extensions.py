import asyncio
import json
import socket
import struct
import time
from dataclasses import replace
from datetime import datetime

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QFileDialog

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.application.diagnostics import DiagnosticRow, request_stats
from all_protocol.application.scenario_model import (
    ConditionalRule, PointRef, ScenarioEvent, ScenarioExtension, ScenarioValue, SequenceStep, check_extension,
)
from all_protocol.application.server_contracts import ListenerState, ServerEvent
from all_protocol.application.server_session import ServerSession
from all_protocol.application.simulation import Assignment, RuleKind, SimulationCommand, SimulationRule, SimulationState, SimulationUpdate
from all_protocol.database import decode_project_database, encode_project_database
from all_protocol.modbus_points import export_station_points, import_station_points
from all_protocol.project import Project, ProjectFile, ProjectSimulator, decode_project, project_record
from all_protocol.scenario_trust import is_trusted, trust
from all_protocol.scenario_worker import compile_source
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import ServerStorage, decode_modbus_plan, encode_modbus_plan
from all_protocol.ui.modbus_server_pane import ModbusServerPane
from all_protocol.ui.scenario_extension_editor import EXAMPLE, ScenarioEditor
from all_protocol.ui.simulation_dialog import SimulationDialog


def config(*, base=False, python=False, multiword=False):
    source = ServerPoint(Area.HR, 0, 0, "指令", DataType.UINT32 if multiword else None)
    output = ServerPoint(Area.HR, 10, 0, "状态", simulation=SimulationRule(RuleKind.FIXED, value=42) if base else None)
    done = ServerPoint(Area.COIL, 0, 0, "完成")
    station = Station(7, "主站点", (source, output, done))
    if python:
        rules = compile_source(EXAMPLE)
    else:
        rules = (ConditionalRule("run", PointRef("HR", 0), 10, (
            SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 1), ScenarioValue(PointRef("Coil", 0), 0))),
            SequenceStep(2000, (ScenarioValue(PointRef("HR", 10), 2),)),
            SequenceStep(5000, (ScenarioValue(PointRef("HR", 10), 3),)),
            SequenceStep(8000, (ScenarioValue(PointRef("HR", 10), 0), ScenarioValue(PointRef("Coil", 0), 1))),
        )),)
    extension = check_extension(station, ScenarioExtension("ext", "生产序列", rules, mode="python" if python else "form",
                                                         source=EXAMPLE if python else ""))
    station = replace(station, extensions=(extension,))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    return ServerPlan(port=port, stations=(station, replace(station, unit_id=8, name="隔离站点")))


class TcpPeer:
    async def connect(self, plan):
        self.reader, self.writer = await asyncio.open_connection(plan.host, plan.port)

    async def request(self, unit, pdu):
        self.writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
        await self.writer.drain()
        async with asyncio.timeout(2):
            header = await self.reader.readexactly(7)
            return await self.reader.readexactly(int.from_bytes(header[4:6], "big") - 1)

    async def write(self, unit, offset, value):
        pdu = struct.pack(">BHH", 6, offset, value)
        assert await self.request(unit, pdu) == pdu

    async def read(self, unit, offset=10):
        result = await self.request(unit, struct.pack(">BHH", 3, offset, 1))
        assert result[:2] == b"\x03\x02"
        return int.from_bytes(result[2:], "big")

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


def test_reset_all_points_stops_scenes_and_restores_unruled_sources():
    async def run():
        plan, events, now = config(base=True), [], 100.0
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration, clock=lambda: now)
        peer = TcpPeer()
        await session.start(plan)
        await peer.connect(plan)
        try:
            await session.control_simulation(SimulationCommand.START)
            await peer.write(7, 0, 10)
            assert await peer.read(7) == 1
            await peer.write(8, 10, 99)
            await session.set_station_active(8, False)
            await session.reset_points()
            assert await peer.read(7, 0) == 0  # Source has no base simulation rule.
            assert await peer.read(7) == 0
            assert session._simulation.state == SimulationState.READY
            assert not session._scenario.running and not session._scenario.heap
            assert not session._simulation.snapshot().held
            assert not session._scenario.arbiter.manual
            now = 200
            session._simulation.tick()
            session._scenario.tick()
            assert await peer.read(7) == 0
            await session.set_station_active(8, True)
            assert await peer.read(8) == 0
            assert not session._pending_point_resets
            assert plan.stations[0].points[0].initial == 0
        finally:
            await peer.close()
            await session.stop()
        await session.reset_points()
        assert session._state == ListenerState.STOPPED
        assert "服务未监听" in events[-1].payload.error
    asyncio.run(run())


def test_selected_reset_cancels_related_sequence_and_keeps_other_station_running():
    async def run():
        plan, now = config(base=True), 100.0
        session = ServerSession(lambda _: None, SERVER_PROTOCOLS[0].registration, clock=lambda: now)
        peer = TcpPeer()
        await session.start(plan)
        await peer.connect(plan)
        try:
            await session.control_simulation(SimulationCommand.START)
            await peer.write(7, 0, 10)
            await peer.write(8, 0, 10)
            assert await peer.read(7) == await peer.read(8) == 1
            await session.reset_point(ModbusServerTarget(7, Area.HR, 10))
            assert await peer.read(7) == 0
            assert await peer.read(7, 0) == 10
            now = 102
            session._simulation.tick()
            session._scenario.tick()
            assert await peer.read(7) == 0
            assert await peer.read(8) == 2
            assert session._scenario.running
        finally:
            await peer.close()
            await session.stop()
    asyncio.run(run())


def test_selected_reset_does_not_trigger_source_condition():
    async def run():
        plan = config()
        stations = []
        for station in plan.stations:
            original = station.extensions[0]
            extension = check_extension(station, replace(original, rules=(replace(original.rules[0], value=0),)))
            stations.append(replace(station, extensions=(extension,)))
        plan = replace(plan, stations=tuple(stations))
        events = []
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration)
        await session.start(plan)
        try:
            await session.control_simulation(SimulationCommand.START)
            source = ModbusServerTarget(7, Area.HR, 0)
            await session.write_current(source, 10)
            await session.reset_point(source)
            session._scenario.tick()
            assert await session.read_current(source) == 0
            assert not any(isinstance(e.payload, ScenarioEvent) and e.payload.action == "started" for e in events)
            await session.reset_point(ModbusServerTarget(7, Area.HR, 999))
            assert "点位不存在" in events[-1].payload.error
            await session.set_station_active(7, False)
            await session.reset_point(source)
            assert "从站未启动" in events[-1].payload.error
            assert not session._pending_point_resets
        finally:
            await session.stop()
    asyncio.run(run())


def test_real_tcp_trigger_sequence_pause_station_isolation_and_cancellation():
    async def run():
        plan = config()
        events, now = [], 100.0
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration, clock=lambda: now)
        peer = TcpPeer()
        await session.start(plan)
        await peer.connect(plan)
        try:
            assert session._simulation is not None  # Extension-only scenes can start.
            await session.control_simulation(SimulationCommand.START)
            await peer.write(7, 0, 10)
            assert await peer.read(7) == 1
            assert await peer.read(8) == 0
            now = 101.999
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 1
            now = 102
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 2
            await session.control_simulation(SimulationCommand.PAUSE)
            now = 202
            assert await peer.read(7) == 2
            await session.control_simulation(SimulationCommand.START)
            now = 205
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 3
            await session.set_station_active(7, False)
            await peer.write(8, 0, 10)
            assert await peer.read(8) == 1
            now = 207
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(8) == 2
            await session.set_station_active(7, True)
            assert await peer.read(7) == 3
            await peer.write(7, 0, 0)
            await peer.write(7, 0, 10)
            assert await peer.read(7) == 1
            await peer.write(7, 10, 99)
            now = 209
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 99
            update = next(e.payload for e in reversed(events) if isinstance(e.payload, SimulationUpdate))
            target = ModbusServerTarget(7, Area.HR, 10)
            assert target in update.held
            await session.control_simulation(SimulationCommand.RELEASE, target)
            await peer.write(7, 0, 0)
            await peer.write(7, 0, 10)
            assert await peer.read(7) == 1
            await session.control_simulation(SimulationCommand.RESET)
            assert await peer.read(7) == 0
            assert await peer.read(8) == 0
        finally:
            await peer.close()
            await session.stop()
        assert any(isinstance(e.payload, ScenarioEvent) and e.payload.action == "cancelled" for e in events)
        await session.start(plan)
        try:
            assert await session.read_current(ModbusServerTarget(7, Area.HR, 10)) == 0
            assert not session._scenario.heap
        finally:
            await session.stop()
    asyncio.run(run())


def test_base_scene_retained_restored_and_extension_writes_do_not_retrigger():
    async def run():
        plan, events = config(base=True), []
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration)
        await session.start(plan)
        target = ModbusServerTarget(7, Area.HR, 10)
        try:
            await session.control_simulation(SimulationCommand.START)
            assert await session.read_current(target) == 42
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 10)
            await asyncio.sleep(0)
            assert await session.read_current(target) == 1
            await session.control_extension(7, "ext", "release")
            await session.control_simulation(SimulationCommand.START)
            assert await session.read_current(target) == 42
            assert plan.stations[0].points[1].simulation.value == 42
            assert len([e for e in events if isinstance(e.payload, ScenarioEvent) and e.payload.action == "started"]) == 1
        finally:
            await session.stop()
    asyncio.run(run())


def test_commits_capture_multiword_partial_write_and_atomic_batch():
    async def run():
        plan = config(multiword=True)
        session = ServerSession(lambda e: None, SERVER_PROTOCOLS[0].registration)
        peer = TcpPeer()
        await session.start(plan)
        await peer.connect(plan)
        try:
            await session.control_simulation(SimulationCommand.START)
            # Low word belongs to a configured UInt32 starting at 0.
            await peer.write(7, 1, 10)
            assert await peer.read(7) == 1
            target = ModbusServerTarget(7, Area.HR, 10)
            coil = ModbusServerTarget(7, Area.COIL, 0)
            with pytest.raises(ValueError):
                session._adapter.write_batch(((target, 123), (coil, 2)))
            assert await session.read_current(target) == 1
            assert await session.read_current(coil) == 0
            response = await peer.request(7, bytes.fromhex("10 0000 0002 04 0000 0000"))
            assert response == bytes.fromhex("10 0000 0002")
            # Capture both sides of a short pulse before one runtime tick.
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 10)
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 0)
            await asyncio.sleep(0)
            assert sum(r.state == "执行中" for r in session._scenario.rules.values()) == 1
        finally:
            await peer.close()
            await session.stop()
    asyncio.run(run())


def test_scenarios_roundtrip_local_project_json_sqlite_and_multisimulator(tmp_path):
    plan = config(base=True)
    storage = ServerStorage(tmp_path)
    storage.save_plan(plan)
    assert storage.load_plan() == plan
    assert encode_modbus_plan(plan)["schema_version"] == 5
    assert decode_modbus_plan(json.loads(json.dumps(encode_modbus_plan(plan)))) == plan
    for simulators in (None, (ProjectSimulator("sim-1", "设备", "modbus-tcp", plan),)):
        project = Project("场景项目", modbus=plan, simulators=simulators)
        record = project_record(project)
        assert record["schema_version"] == 9
        assert decode_project(json.loads(json.dumps(record))) == project
        assert decode_project(decode_project_database(encode_project_database(record))) == project
        path = tmp_path / ("multi.allprotocol" if simulators else "legacy.allprotocol")
        ProjectFile().save(project, path)
        assert ProjectFile().open(path) == project
    station = plan.stations[0]
    exported = export_station_points(station)
    assert "extensions" not in exported and "build(" not in exported
    assert import_station_points(exported) == tuple(replace(p, data_type=p.value_type) for p in station.points)
    plain = replace(plan, stations=tuple(replace(s, extensions=()) for s in plan.stations))
    storage.save_plan(plain)
    assert storage.load_plan() == plain  # Metadata must not resurrect removed scenarios.


def test_python_requires_local_check_permission_and_runs_checked_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("ALL_PROTOCOL_SCENARIO_TRUST_DIR", str(tmp_path))
    plan = config(python=True)
    extension = plan.stations[0].extensions[0]
    assert not is_trusted(extension)
    async def run():
        session = ServerSession(lambda e: None, SERVER_PROTOCOLS[0].registration)
        await session.start(plan)
        try:
            await session.control_simulation(SimulationCommand.START)
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 10)
            await asyncio.sleep(0)
            assert await session.read_current(ModbusServerTarget(7, Area.HR, 10)) == 0
            trust(extension)
            await session.control_extension(7, extension.id, "enable")
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 0)
            await session.write_current(ModbusServerTarget(7, Area.HR, 0), 10)
            await asyncio.sleep(0)
            assert await session.read_current(ModbusServerTarget(7, Area.HR, 10)) == 1
        finally:
            await session.stop()
    asyncio.run(run())


def test_scenario_logs_roundtrip_clear_and_request_statistics(tmp_path):
    payload = ScenarioEvent(7, "ext", "run", 1, "step", "步骤 1", 1, 2000, 2010, 10)
    event = ServerEvent("modbus-tcp", "127.0.0.1:1502", datetime.now(), payload)
    storage = ServerStorage(tmp_path)
    storage.append_event(event)
    assert storage.recent_events() == (event,)
    row = DiagnosticRow.from_server(event)
    assert row.operation == "场景" and not row.packets
    assert request_stats([row]).count == 0
    storage.clear_log()
    storage.append_event(replace(event, time=datetime.now()))
    assert len(storage.recent_events()) == 1


class Bridge(QObject):
    event = Signal(object)


def test_dialog_unit_filter_edits_correct_target_and_preserves_base(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    plan = config(base=True)
    storage = ServerStorage(tmp_path)
    storage.save_plan(plan)
    pane = ModbusServerPane(Bridge(), storage)
    pane.station_list.setCurrentRow(1)
    dialog = SimulationDialog(pane)
    try:
        assert dialog.group.currentData() == 8
        assert dialog.point.count() == 3
        assert all(dialog.point.itemData(i).unit_id == 8 for i in range(3))
        dialog.point.setCurrentIndex(1)
        dialog._set_rule(SimulationRule(RuleKind.FIXED, value=84))
        assert pane.configuration_plan().stations[0].points[1].simulation.value == 42
        assert pane.configuration_plan().stations[1].points[1].simulation.value == 84
        assert pane.configuration_plan().stations[1].extensions == plan.stations[1].extensions
        dialog.group.setCurrentIndex(dialog.group.findData(7))
        assert all(dialog.point.itemData(i).unit_id == 7 for i in range(3))
        assert dialog.extensions.station().unit_id == 7
        pane._save_plan()
        assert storage.load_plan().stations[1].points[1].simulation.value == 84
    finally:
        dialog.close()
        pane.close()
        app.processEvents()


def test_form_editor_and_python_worker_preview_are_scoped_and_invalidate_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("ALL_PROTOCOL_SCENARIO_TRUST_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    station = config().stations[0]
    saved = []
    dialog = ScenarioEditor(None, station, station.extensions[0], saved.append)
    try:
        dialog.check_definition()
        assert dialog.apply.isEnabled(), dialog.error.text()
        assert "Unit 7/HR/10" in dialog.preview.toPlainText()
        assert all("Unit 7" in dialog.source.itemText(i) for i in range(dialog.source.count()))
        dialog.condition.setText("11")
        assert not dialog.apply.isEnabled()
        dialog.check_definition()
        assert dialog.checked.rules[0].value == 11
        dialog.tabs.setCurrentIndex(1)
        dialog.check_definition()
        assert "可信" in dialog.error.text()
        dialog.trusted.setChecked(True)
        dialog.check_definition()
        deadline = time.monotonic() + 6
        while not dialog.apply.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert dialog.apply.isEnabled(), dialog.error.text()
        dialog._apply()
        assert saved and saved[0].mode == "python" and is_trusted(saved[0])
    finally:
        dialog.close()
        app.processEvents()


def test_clear_journal_keeps_running_sequence_and_records_later_steps(qt_application, tmp_path):
    from functools import partial
    from all_protocol.ui.server_io_bridge import ServerIoBridge
    from all_protocol.reporting import _record

    def wait_until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(.001)
        assert predicate()

    now = [100.0]
    storage = ServerStorage(tmp_path)
    storage.save_plan(config())
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration,
                                   clock=lambda: now[0]), ServerSession.stop, storage)
    pane = ModbusServerPane(bridge, storage)
    try:
        pane.listen.click()
        wait_until(lambda: pane.state == ListenerState.LISTENING)
        async def trigger():
            await bridge.session.control_simulation(SimulationCommand.START)
            await bridge.session.write_current(ModbusServerTarget(7, Area.HR, 0), 10)
        bridge.submit(trigger())
        wait_until(lambda: any(isinstance(e.payload, ScenarioEvent) and e.payload.action == "step"
                               for e in pane.requests.items))
        pane.journal_view.clear_button.click()
        wait_until(pane.journal_view.clear_button.isEnabled)
        assert not pane.requests.items
        now[0] = 102.0
        wait_until(lambda: any(isinstance(e.payload, ScenarioEvent) and e.payload.step == 2
                               for e in pane.requests.items))
        assert pane.state == ListenerState.LISTENING
        retained = storage.recent_events()
        assert len(retained) == 1 and retained[0].payload.planned_ms == 2000
        row = DiagnosticRow.from_server(retained[0])
        record = _record(retained[0], row, "modbus-tcp", datetime.now().astimezone().tzinfo, packets=True)
        assert record["kind"] == "scenario" and not record["packets"]
        assert request_stats([row]).count == 0
    finally:
        bridge.join()
        qt_application.processEvents()
        pane.close()
        pane.deleteLater()


def point_copy_plan(*, unconditional=False):
    plan = config()
    stations = []
    for station in plan.stations:
        rule = replace(station.extensions[0].rules[0],
                       source=None if unconditional else PointRef("HR", 0),
                       op="always" if unconditional else "eq",
                       steps=(SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 0, PointRef("HR", 0)),)),
                              SequenceStep(2000, (ScenarioValue(PointRef("HR", 10), 0, PointRef("HR", 0)),))))
        extension = check_extension(station, replace(station.extensions[0], rules=(rule,)))
        stations.append(replace(station, extensions=(extension,)))
    return replace(plan, stations=tuple(stations))


@pytest.mark.parametrize("unconditional", (False, True))
def test_point_copy_and_unconditional_roundtrip_plan_and_project(tmp_path, unconditional):
    plan = point_copy_plan(unconditional=unconditional)
    storage = ServerStorage(tmp_path)
    storage.save_plan(plan)
    assert storage.load_plan() == plan
    record = encode_modbus_plan(plan)
    assert record["schema_version"] == 6
    assert decode_modbus_plan(json.loads(json.dumps(record))) == plan
    with pytest.raises(ValueError, match="方案 v6"):
        decode_modbus_plan(record | {"schema_version": 5})
    for simulators in (None, (ProjectSimulator("copy", "复制", "modbus-tcp", plan),)):
        project = Project("点位复制", modbus=plan if simulators is None else ServerPlan(), simulators=simulators)
        record = project_record(project)
        assert record["schema_version"] == 10
        with pytest.raises(ValueError, match="项目 v10"):
            decode_project(record | {"schema_version": 9})
        path = tmp_path / ("copy.allprotocol" if simulators is None else "copy-multi.allprotocol")
        ProjectFile().save(project, path)
        assert ProjectFile().open(path) == project


@pytest.mark.parametrize("unconditional", (False, True))
def test_real_tcp_copies_current_point_value_with_station_isolation(unconditional):
    async def run():
        plan = point_copy_plan(unconditional=unconditional)
        now = [100.0]
        session = ServerSession(lambda _: None, SERVER_PROTOCOLS[0].registration, clock=lambda: now[0])
        peer = TcpPeer()
        await session.start(plan)
        await peer.connect(plan)
        try:
            if unconditional:
                await peer.write(7, 0, 12)
            await session.control_simulation(SimulationCommand.START)
            if unconditional:
                assert await peer.read(7) == 12
            else:
                await peer.write(7, 0, 10)
                assert await peer.read(7) == 10
            assert await peer.read(8) == 0
            await peer.write(7, 0, 35)
            now[0] = 102
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 35
            assert await peer.read(8) == 0
            await session.control_simulation(SimulationCommand.PAUSE)
            await peer.write(7, 0, 70)
            await session.control_simulation(SimulationCommand.START)
            assert await peer.read(7) == 35  # A resume does not replay the rule.
        finally:
            await peer.close()
            await session.stop()
    asyncio.run(run())


def test_form_selects_value_point_unconditional_and_restores_saved_fields(qt_application):
    station = replace(config().stations[0], extensions=())
    saved = []
    dialog = ScenarioEditor(None, station, None, saved.append)
    reopened = None
    try:
        target = dialog.steps.cellWidget(0, 1)
        target.setCurrentIndex(next(i for i in range(target.count()) if target.itemData(i) == PointRef("HR", 10)))
        mode = dialog.steps.cellWidget(0, 2)
        stack = dialog.steps.cellWidget(0, 3)
        stack.widget(0).setText("123")
        mode.setCurrentIndex(mode.findData("point"))
        picker = stack.widget(1)
        # The value source can differ from the condition source.
        picker.setCurrentIndex(next(i for i in range(picker.count()) if picker.itemData(i) == PointRef("Coil", 0)))
        dialog.check_definition()
        assert dialog.apply.isEnabled(), dialog.error.text()
        assert "Unit 7/Coil/0 的当前值" in dialog.preview.toPlainText()
        assert dialog.checked.rules[0].steps[0].values[0].value_source == PointRef("Coil", 0)
        mode.setCurrentIndex(mode.findData("literal"))
        assert not dialog.apply.isEnabled()
        dialog.check_definition()
        item = dialog.checked.rules[0].steps[0].values[0]
        assert item.value == 123 and item.value_source is None
        mode.setCurrentIndex(mode.findData("point"))
        dialog.compare.setCurrentIndex(dialog.compare.findData("always"))
        dialog.condition.setText("无需填写")
        assert not dialog.source.isEnabled() and not dialog.condition.isEnabled()
        assert not dialog.initial.isEnabled() and not dialog.cancel_false.isEnabled()
        dialog.check_definition()
        assert dialog.apply.isEnabled(), dialog.error.text()
        assert "无条件（启动场景时执行一次）" in dialog.preview.toPlainText()
        dialog._apply()
        assert saved[0].rules[0].source is None
        reopened = ScenarioEditor(None, station, saved[0], lambda _: None)
        assert reopened.compare.currentData() == "always"
        assert reopened.steps.cellWidget(0, 2).currentData() == "point"
        assert reopened.steps.cellWidget(0, 3).widget(1).currentData() == PointRef("Coil", 0)
        reopened.check_definition()
        assert reopened.checked == saved[0]
        reopened.steps.cellWidget(0, 3).widget(1).setCurrentIndex(0)
        assert not reopened.apply.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        if reopened:
            reopened.close()
            reopened.deleteLater()
        qt_application.processEvents()


def test_unconditional_editor_with_no_points_reports_error(qt_application):
    dialog = ScenarioEditor(None, Station(1), None, lambda _: None)
    try:
        dialog.compare.setCurrentIndex(dialog.compare.findData("always"))
        dialog.check_definition()
        assert not dialog.apply.isEnabled()
        assert "请选择" in dialog.error.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        qt_application.processEvents()


def test_point_value_extension_package_export_import_and_version_guard(qt_application, tmp_path, monkeypatch):
    storage = ServerStorage(tmp_path)
    storage.save_plan(point_copy_plan(unconditional=True))
    pane = ModbusServerPane(Bridge(), storage)
    dialog = SimulationDialog(pane)
    path = tmp_path / "scenario.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(path), ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    try:
        widget = dialog.extensions
        widget.set_unit(7)
        widget._export()
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["version"] == 2
        path.write_text(json.dumps(record | {"version": 1}), encoding="utf-8")
        widget._import()
        assert "扩展包 v2" in widget.status.text()
        assert len(widget.station().extensions) == 1
        path.write_text(json.dumps(record), encoding="utf-8")
        widget._import()
        extensions = widget.station().extensions
        assert len(extensions) == 2
        assert not extensions[1].enabled and not extensions[1].checked_digest
        assert extensions[1].id != extensions[0].id
        assert extensions[1].rules == extensions[0].rules
    finally:
        dialog.close()
        pane.close()
        qt_application.processEvents()
