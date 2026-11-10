from dataclasses import replace
import gc
from time import monotonic

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QInputDialog, QMessageBox

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Target
from all_protocol.application.contracts import ClientError, ConnectionState, ErrorKind, OperationRejected, Point, ProtocolRegistration
from all_protocol.point_tables import PointDefinition, PointTable, export_points_csv, export_table_json, new_id
from all_protocol.project import Project, ProjectFile, ProjectHistory, ProjectWorkspace
from all_protocol.storage import ConnectionProfile, LocalHistory
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import DesktopProtocol, MainWindow
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.s7_form import S7Form


def wait_for(condition):
    deadline = monotonic() + 3
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


@pytest.fixture(params=("modbus-tcp", "s7"))
def client(request, monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    protocol = request.param
    factory = ModbusTcpForm if protocol == "modbus-tcp" else S7Form
    form = factory()
    form.connection.host.setText("127.0.0.1")
    form.query.count.setValue(1)
    fields = form.profile_fields()
    config, endpoint = form.connection_config()
    workspace = ProjectWorkspace(Project("测试项目").remember(ConnectionProfile(protocol, endpoint, fields, 1000)))
    device_id = workspace.project.devices[0].id
    target = (ModbusTarget(1, Area.HR, 0, 1) if protocol == "modbus-tcp" else S7Target(S7Area.DB, 1, 0, S7Type.WORD))
    points = tuple(PointDefinition(new_id(), name, replace(target, offset=offset), "℃", 0.1, precision=1)
                   for name, offset in (("温度", 0), ("无效地址", 99), ("反馈", 4)))
    table = PointTable(new_id(), device_id, "运行状态", points)
    workspace.project = workspace.project.with_table(table).with_table(PointTable(new_id(), device_id, "报警"))
    writes = []

    class FakeClient:
        def __init__(self, actual, _events):
            assert actual == config

        async def connect(self):
            pass

        async def close(self):
            pass

        def function(self, command):
            return "write" if command.values is not None else "read"

        async def read(self, target):
            if target.offset == 99:
                raise ClientError(ErrorKind.PROTOCOL, "未定义地址")
            return (Point(target.label, "raw", 235 if target.offset == 0 else 125),)

        async def write(self, target, values):
            writes.append((target, values))

    bridge = IoBridge()
    history = ProjectHistory(workspace, LocalHistory(tmp_path / "local"))
    window = MainWindow(bridge, (DesktopProtocol(ProtocolRegistration(protocol, protocol, FakeClient, True), factory),), history)
    window.show()
    window.point_views.setCurrentIndex(1)
    yield app, window, workspace, writes, protocol
    bridge.join()
    window.close()
    app.processEvents()
    window.deleteLater()
    bridge.deleteLater()
    form.connection.deleteLater()
    form.query.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def test_table_management_and_project_round_trip(client, monkeypatch, tmp_path):
    _app, window, workspace, _writes, _protocol = client
    pane = window.point_table_pane
    assert pane.tables.count() == 2
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("配方", True))
    pane.actions["新建点表"].click()
    assert pane.table.name == "配方"
    original_id = pane.table.id
    pane.actions["复制"].click()
    assert pane.table.name == "配方" and pane.table.id != original_id
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("配方副本", True))
    pane.actions["重命名"].click()
    assert pane.table.name == "配方副本"
    before = workspace.project
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    pane.actions["删除点表"].click()
    assert workspace.project == before
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    pane.actions["删除点表"].click()
    assert len(workspace.project.point_tables) == 3
    path = tmp_path / "project.json"
    ProjectFile().save(workspace.project, path)
    assert ProjectFile().open(path) == workspace.project


def test_point_write_previews_engineering_value_and_displays_separate_readback(client):
    app, window, _workspace, writes, _protocol = client
    pane = window.point_table_pane
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    pane.point_table.selectRow(0)

    def submit():
        dialog = app.activeModalWidget()
        try:
            dialog.mode.setCurrentText("工程值")
            dialog.input.setPlainText("4.25")
            dialog.accept()
            assert "不是整数" in dialog.error.text() and not writes
            dialog.input.setPlainText("4.2")
            assert "原始值  42" in dialog.preview.toPlainText()
            assert "工程值  4.2 ℃" in dialog.preview.toPlainText()
            dialog.auto_verify.setChecked(True)
            dialog.accept()
        except Exception:
            dialog.reject()
            raise

    QTimer.singleShot(0, submit)
    pane.actions["写入点位…"].click()
    wait_for(lambda: "回读不一致" in window.verification_state.text())
    assert "设备已应答" in window.write_state.text()
    assert writes == [(pane.point.target, (42,))]
    verification = window.journal.items[0]
    window.log_table.setCurrentIndex(window.journal.index(0, 0))
    assert "预期值 42" in window.detail.toPlainText()
    assert "回读值 235" in window.detail.toPlainText()
    assert verification.verification.write_request_id == window.journal.items[1].request_id
    window.verify_latest.click()
    wait_for(lambda: "独立读取" in window.verification_state.text())
    assert len(writes) == 1


def test_edit_point_and_bulk_preview_are_atomic(client):
    app, window, workspace, _writes, protocol = client
    pane = window.point_table_pane

    def add_point():
        dialog = app.activeModalWidget()
        dialog.name.setText("压力")
        dialog.unit.setText("bar")
        dialog.scale.setText("0.01")
        dialog.offset.setText("-1")
        dialog.target_form.query.offset.setValue(8)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, add_point)
    pane.actions["新增点位"].click()
    assert len(pane.table.points) == 4 and pane.table.points[-1].name == "压力"
    pane.point_table.selectRow(3)
    point_id = pane.point.id

    def edit_point():
        dialog = app.activeModalWidget()
        dialog.name.setText("出口压力")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, edit_point)
    pane.actions["编辑点位"].click()
    assert pane.table.points[-1].id == point_id and pane.table.points[-1].name == "出口压力"
    before = workspace.project

    def bulk_preview():
        dialog = app.activeModalWidget()
        dialog.input.setPlainText("name,area,address_offset,data_type\nbad,UNKNOWN,0,invalid\n")
        dialog._preview()
        assert "第 2 行" in dialog.error.text()
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        assert workspace.project == before
        edited = tuple(replace(point, unit="kPa") for point in pane.table.points)
        dialog.input.setPlainText(export_points_csv(edited))
        dialog._preview()
        assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        assert workspace.project == before
        dialog.input.insertPlainText(" ")
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        dialog.input.setPlainText(export_points_csv(edited))
        dialog._preview()
        dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, bulk_preview)
    pane.actions["批量编辑…"].click()
    assert all(point.unit == "kPa" for point in pane.table.points)
    assert {point.id for point in pane.table.points} == {point.id for point in before.point_tables[0].points}


def test_import_cancel_bad_csv_and_success_and_export(client, monkeypatch, tmp_path):
    app, window, workspace, _writes, protocol = client
    pane = window.point_table_pane
    path = tmp_path / "table.json"
    path.write_text(export_table_json(pane.table, protocol), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
    before = workspace.project
    QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
    pane.actions["导入…"].click()
    assert workspace.project == before
    QTimer.singleShot(0, lambda: app.activeModalWidget().accept())
    pane.actions["导入…"].click()
    assert len(workspace.project.point_tables) == 3
    assert pane.table.id != before.point_tables[0].id
    assert pane.table.points[0].id != before.point_tables[0].points[0].id
    exported = tmp_path / "export.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(exported), ""))
    pane.actions["导出 JSON…"].click()
    assert exported.exists() and "工程值" not in exported.read_text(encoding="utf-8")
    csv = tmp_path / "bad.csv"
    csv.write_text("name,area,address_offset,data_type\nx,UNKNOWN,0,invalid\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(csv), ""))
    before = workspace.project
    pane.actions["导入…"].click()
    assert workspace.project == before and "第 2 行" in pane.feedback.text()


def test_read_table_failure_isolated_raw_write_and_address_change_clears_sample(client):
    app, window, workspace, writes, _protocol = client
    pane = window.point_table_pane
    assert not pane.actions["读取整表"].isEnabled()
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    pane.actions["读取整表"].click()
    wait_for(lambda: not window._state.busy)
    model = pane.point_table.model()
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "235"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "23.5"
    assert model.data(model.index(1, 6), Qt.ItemDataRole.DisplayRole) == "协议失败"
    assert model.data(model.index(2, 2), Qt.ItemDataRole.DisplayRole) == "125"
    assert window.results.rowCount() == 0
    assert len([entry for entry in window.journal.items if entry.operation == "读取"]) == 3
    pane.point_table.selectRow(0)

    def confirm_write():
        dialog = app.activeModalWidget()
        dialog.input.setPlainText("42")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, confirm_write)
    pane.actions["写入点位…"].click()
    wait_for(lambda: writes and not window._state.busy)
    assert writes == [(pane.table.points[0].target, (42,))]
    previous = pane.table.points[0]
    changed = replace(previous, target=replace(previous.target, offset=8))
    pane._apply(replace(pane.table, points=(changed,) + pane.table.points[1:]))
    assert pane.rows.data(pane.rows.index(0, 2), Qt.ItemDataRole.DisplayRole) == "—"
    pane._apply(replace(pane.table, points=(previous,) + pane.table.points[1:]))
    assert pane.rows.data(pane.rows.index(0, 2), Qt.ItemDataRole.DisplayRole) == "—"
    window.connection_button.click()
    wait_for(lambda: window._state.connection == ConnectionState.OFFLINE)
    assert "旧数据" in pane.rows.data(pane.rows.index(2, 6), Qt.ItemDataRole.DisplayRole)
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    assert "旧数据" in pane.rows.data(pane.rows.index(2, 6), Qt.ItemDataRole.DisplayRole)


def test_device_delete_warns_about_tables_and_cancel_preserves_them(client):
    app, window, workspace, _writes, _protocol = client
    before = workspace.project

    def choose(role):
        dialog = app.activeModalWidget()
        assert "2 张点表" in dialog.text()
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == role).click()

    QTimer.singleShot(0, lambda: choose(QMessageBox.ButtonRole.RejectRole))
    window.delete_recent_button.click()
    assert workspace.project == before
    QTimer.singleShot(0, lambda: choose(QMessageBox.ButtonRole.DestructiveRole))
    window.delete_recent_button.click()
    assert not workspace.project.devices and not workspace.project.point_tables
    assert window.point_table_pane.rows.rowCount() == 0


def test_empty_project_can_save_device_and_add_table_without_connecting(client, monkeypatch):
    _app, window, workspace, _writes, _protocol = client
    pane = window.point_table_pane
    workspace.project = Project("空项目")
    window._refresh()
    assert not pane.actions["新建点表"].isEnabled()
    assert pane.save_device_button.isVisible()
    pane.save_device_button.click()
    assert len(workspace.project.devices) == 1
    assert window._state.connection == ConnectionState.OFFLINE
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("新表", True))
    pane.actions["新建点表"].click()
    assert pane.table.name == "新表" and not pane.table.points
    assert not pane.actions["读取整表"].isEnabled()


def test_read_only_region_cannot_offer_write(client):
    _app, window, _workspace, _writes, protocol = client
    pane = window.point_table_pane
    original = pane.table.points[0]
    readonly = (replace(original.target, area=Area.IR) if protocol == "modbus-tcp"
                else replace(original.target, area=S7Area.I, db=0))
    pane._apply(replace(pane.table, points=(replace(original, target=readonly),)))
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    pane.point_table.selectRow(0)
    assert pane.actions["读取整表"].isEnabled()
    assert not pane.actions["写入点位…"].isEnabled()


def test_independent_table_polling_accepts_manual_write_and_preserves_selection(client):
    app, window, workspace, writes, _protocol = client
    pane = window.point_table_pane
    first, second = workspace.project.point_tables
    second_point = replace(first.points[0], id=new_id(), name="报警值", target=replace(first.points[0].target, offset=8))
    workspace.project = workspace.project.with_table(replace(first, interval_ms=200)).with_table(
        replace(second, interval_ms=400, points=(second_point,)))
    window._refresh()
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    pane.poll_button.click()
    wait_for(lambda: first.id in window._state.polling_groups)
    assert not pane.actions["编辑点位"].isEnabled()
    pane.tables.setCurrentIndex(pane.tables.findData(second.id))
    pane.poll_button.click()
    wait_for(lambda: len(window._state.polling_groups) == 2)
    wait_for(lambda: pane.samples.samples[second.id, second_point.id].request_id is not None)
    initial_request = pane.samples.samples[second.id, second_point.id].request_id
    pane.point_table.selectRow(0)
    resets = QSignalSpy(pane.rows.modelReset)

    def confirm():
        dialog = app.activeModalWidget()
        dialog.input.setPlainText("42")
        # A real polling cycle runs while the user is considering the write.
        QTimer.singleShot(250, lambda: dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click())

    QTimer.singleShot(0, confirm)
    pane.actions["写入点位…"].click()
    wait_for(lambda: bool(writes))
    assert writes[0] == (second_point.target, (42,))
    wait_for(lambda: pane.samples.samples[second.id, second_point.id].request_id > initial_request)
    assert pane.point.id == second_point.id and resets.count() == 0
    pane.stop_all_button.click()
    wait_for(lambda: not window._state.polling_groups)
    assert pane.tables.currentData() == second.id


def test_expired_samples_and_settings_persist_without_model_reset(client, tmp_path):
    _app, window, workspace, _writes, _protocol = client
    pane = window.point_table_pane
    tick = [0.0]
    pane.samples.clock = lambda: tick[0]
    pane.poll_interval.setValue(250)
    pane.stale_after.setValue(600)
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    pane.actions["读取整表"].click()
    wait_for(lambda: not window._state.busy)
    pane.point_table.selectRow(0)
    point_id = pane.point.id
    resets = QSignalSpy(pane.rows.modelReset)
    tick[0] = .6
    pane._flush_values()
    assert pane.rows.data(pane.rows.index(0, 6), Qt.ItemDataRole.DisplayRole) == "过期 · 旧数据"
    assert pane.rows.data(pane.rows.index(0, 2), Qt.ItemDataRole.DisplayRole) == "235"
    assert pane.point.id == point_id and resets.count() == 0
    path = tmp_path / "scheduled.json"
    ProjectFile().save(workspace.project, path)
    restored = ProjectFile().open(path).point_tables[0]
    assert (restored.interval_ms, restored.stale_after_ms) == (250, 600)
    assert "raw_value" not in path.read_text(encoding="utf-8")


def test_queue_full_and_rejection_are_visible_without_losing_configuration(client):
    _app, window, workspace, _writes, _protocol = client
    pane = window.point_table_pane
    window.connection_button.click()
    wait_for(window.read_button.isEnabled)
    before = workspace.project
    state = window._state
    window._handle_event(replace(state, queued=state.queue_capacity))
    assert "队列已满" in pane.schedule_status.text()
    assert not window.read_button.isEnabled() and not pane.actions["读取整表"].isEnabled()
    window._handle_event(OperationRejected("写入", "队列已满，请求未发送", state.generation))
    assert "未发送" in window.statusBar().currentMessage() and "未发送" in pane.feedback.text()
    window._handle_event(state)
    assert window.read_button.isEnabled() and workspace.project == before
