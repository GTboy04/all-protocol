from dataclasses import replace
import socket
from time import monotonic

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTreeWidgetItemIterator

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import ConnectionState, ProtocolRegistration
from all_protocol.application.faults import FaultAction
from all_protocol.application.server_contracts import ListenerState
from all_protocol.application.simulation import RuleKind, SimulationState
from all_protocol.point_tables import PointDefinition, PointTable
from all_protocol.project import Project, ProjectDevice, ProjectFile, ProjectWorkspace, WorkspaceLayout
from all_protocol.storage import ConnectionProfile, VISIBLE_LOG_ROWS
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.fault_dialog import FaultDialog
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.s7_form import S7Form
from all_protocol.ui.simulation_dialog import RuleDialog, SimulationDialog
from all_protocol.ui.style import STYLE


def wait_for(condition):
    deadline = monotonic() + 5
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert condition()


def ports():
    with socket.socket() as first, socket.socket() as second:
        for sock in (first, second):
            sock.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mb_port, s7_port = ports()
    mb = ConnectionProfile("modbus-tcp", f"127.0.0.1:{mb_port}", {
        "host": "127.0.0.1", "port": mb_port, "timeout_ms": 1000, "unit_id": 1, "area": "HR",
        "offset": 0, "count": 1, "data_type": "UInt16", "order": "big",
    }, 200)
    s7 = ConnectionProfile("s7", f"127.0.0.1:{s7_port} · Rack 0 / Slot 1", {
        "host": "127.0.0.1", "port": s7_port, "timeout_ms": 1000, "rack": 0, "slot": 1,
        "area": "DB", "db": 1, "offset": 0, "count": 1, "data_type": "Word", "bit": 0,
    }, 200)
    target = S7Target(S7Area.DB, 1, 0, DataType.WORD)
    shared_name = "同名设备 " + "生产线远程输入监测" * 17
    project = Project(
        "工作区验收", (ProjectDevice("mb", shared_name, mb), ProjectDevice("s7", shared_name, s7)),
        ServerPlan(port=mb_port, stations=(Station(1, "温控", (ServerPoint(Area.HR, 0, 42),)),)),
        S7ServerPlan(port=s7_port, points=(S7ServerPoint(target, 125),)),
        (PointTable("mb-table", "mb", "温度", (PointDefinition("mb-point", "温度", ModbusTarget(1, Area.HR, 0, 1)),), 200),
         PointTable("s7-table", "s7", "反馈", (PointDefinition("s7-point", "反馈", target),), 200)),
    )
    protocols = (
        DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", ModbusTcpClient, True), ModbusTcpForm),
        DesktopProtocol(ProtocolRegistration("s7", "Siemens S7", S7Client, True), S7Form),
    )
    home = RoleWindow(protocols, tmp_path / "local")
    file = ProjectFile()
    path = tmp_path / "workspace.allprotocol.json"
    file.save(project, path)
    home.workspace = ProjectWorkspace(project, file)
    home._refresh_project()
    home.show()
    home.project_entry.click()
    home.project_window.setStyleSheet(STYLE)
    app.processEvents()
    try:
        yield app, home, path
    finally:
        home.join()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        home.workspace = None
        home.close()
        home.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def select(window, kind, key):
    window.tree.setCurrentItem(window._items[kind, key])


def test_project_diagnostics_highlights_raw_bytes_while_sampling_and_shows_fault_status(workspace):
    app, home, _ = workspace
    window = home.project_window
    client = window.client
    pane = next(pane for registration, pane in window.servers.panes() if registration.key == "modbus-tcp")
    assert not pane.fault_button.isEnabled()
    pane.listen.click()
    wait_for(pane.fault_button.isEnabled)
    select(window, "device", "mb")
    client.connection_button.click()
    wait_for(lambda: client._state.connection == ConnectionState.ONLINE)
    select(window, "table", "mb-table")
    client.point_table_pane.poll_button.click()
    wait_for(lambda: any(entry.operation == "读取" for entry in client.journal.items))
    entry = next(entry for entry in client.journal.items if entry.operation == "读取")
    client.log_table.setCurrentIndex(client.journal.index(client.journal.items.index(entry), 0))
    incomplete = replace(entry, request_id=9000, packets=((True, b"\x00\x01\x00"),))
    client._all_entries.append(incomplete)
    client._all_entries.extend(replace(entry, request_id=entry.request_id + 10000 + i)
                               for i in range(VISIBLE_LOG_ROWS - len(client._all_entries)))
    button = next(button for button in client.journal_panel.findChildren(QPushButton)
                  if button.text() == "请求诊断…")
    button.click()
    dialog = client._diagnostics
    assert dialog.device.currentData() == ("mb",)
    assert dialog.model.rowCount() == VISIBLE_LOG_ROWS
    assert entry.session_id in dialog.context.text()
    item = QTreeWidgetItemIterator(dialog.tree)
    while item.value() and item.value().text(0) != "功能码":
        item += 1
    assert item.value() is not None
    dialog.tree.setCurrentItem(item.value())
    assert dialog.hex.extraSelections()[0].cursor.selectedText() == "03"
    incomplete_row = next(i for i, (key, _) in enumerate(dialog.model.items) if key == id(incomplete))
    dialog.table.setCurrentIndex(dialog.model.index(incomplete_row, 0))
    error = dialog.tree.topLevelItem(dialog.tree.topLevelItemCount() - 1)
    assert error.text(0) == "解析失败" and "偏移 3" in error.text(1)
    assert dialog.hex.toPlainText().endswith("00 01 00")

    def apply_fault():
        form = app.activeModalWidget()
        assert isinstance(form, FaultDialog)
        form.action.setCurrentText(FaultAction.EXCEPTION)
        form.operation.setCurrentText("读取")
        form.accept()

    QTimer.singleShot(0, apply_fault)
    pane.fault_button.click()
    wait_for(lambda: "已触发 1 次" in pane.fault_status.text())
    assert "已结束" in pane.fault_status.text()
    dialog.error.setCurrentIndex(dialog.error.findData("协议异常"))
    wait_for(lambda: dialog.model.rowCount() == 1)
    assert "失败 100.0%" in dialog.stats.text()
    wait_for(lambda: client._sample_stores["mb"].samples["mb-table", "mb-point"].request_id > entry.request_id + 1)
    dialog.close()
    pane.reset_fault.click()
    wait_for(lambda: pane.fault_status.text() == "故障未启用")


def test_two_clients_two_listeners_switch_cancel_close_and_restore_without_running(workspace):
    app, home, path = workspace
    window = home.project_window
    client = window.client
    panes = {registration.key: pane for registration, pane in window.servers.panes()}
    with socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", panes["s7"].port.value()))
        occupied.listen()
        select(window, "server", "s7")
        panes["s7"].listen.click()
        wait_for(lambda: window.error_button.text() == "异常 1")
        select(window, "device", "mb")
        window.error_button.click()
        assert window.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == ("server", "s7")
        assert "启动失败" in window.issue.toolTip()
    for key, pane in panes.items():
        select(window, "server", key)
        assert window.logs.currentWidget() is pane.journal_panel
        pane.listen.click()
    wait_for(lambda: window.listeners.text() == "监听 2/2")
    assert window.error_button.text() == "异常 0"
    select(window, "device", "mb")
    client.connect_all_button.click()
    wait_for(lambda: window.connections.text() == "连接 2/2")
    for key in ("mb-table", "s7-table"):
        select(window, "table", key)
        assert client.point_table_pane.table.id == key
        client.point_table_pane.poll_button.click()

    def sample(device):
        return client._sample_stores[device].samples.get((device + "-table", device + "-point"))

    wait_for(lambda: all((value := sample(device)) is not None and value.raw_value == expected
                         for device, expected in (("mb", 42), ("s7", 125))))
    assert client.journal.items and all(entry.device_id == "s7" for entry in client.journal.items)
    select(window, "table", "mb-table")
    assert client.journal.items and all(entry.device_id == "mb" for entry in client.journal.items)
    assert window.logs.currentWidget() is client.journal_panel
    client.log_table.setCurrentIndex(client.journal.index(0, 0))
    assert "TX" in client.detail.toPlainText() and "RX" in client.detail.toPlainText()
    select(window, "server", "s7")
    window.server_view.setCurrentIndex(1)
    assert panes["s7"].point_table.isColumnHidden(3) and not panes["s7"].point_table.isColumnHidden(4)
    assert panes["s7"].host.isReadOnly() and panes["s7"].port.isReadOnly()
    assert window.logs.currentWidget() is panes["s7"].journal_panel
    before = (sample("mb").request_id, sample("s7").request_id)
    window.navigation.moveSplitter(350, 1)
    window.vertical.moveSplitter(480, 1)
    window.log_button.click()
    assert window.logs.isHidden() and window.vertical.sizes()[1] < 70

    def cancel_dirty_close():
        dialog = app.activeModalWidget()
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == QMessageBox.ButtonRole.RejectRole).click()

    QTimer.singleShot(0, cancel_dirty_close)
    assert not window.close()
    wait_for(lambda: sample("mb").request_id > before[0] and sample("s7").request_id > before[1])
    assert window.connections.text() == "连接 2/2" and window.listeners.text() == "监听 2/2"
    assert home._save_project()
    saved = ProjectFile().open(path)
    assert saved.layout.selection == ("server", "s7") and not saved.layout.journal_visible
    assert saved.layout.navigation != 220 and saved.layout.journal != 290

    def confirm_clean_close():
        dialog = app.activeModalWidget()
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()

    QTimer.singleShot(0, confirm_clean_close)
    window.close()
    wait_for(lambda: home.project_window is None)
    for port in (saved.modbus.port, saved.s7.port):
        with socket.socket() as probe:
            probe.settimeout(.2)
            assert probe.connect_ex(("127.0.0.1", port)) != 0

    file = ProjectFile()
    home.workspace = ProjectWorkspace(file.open(path), file)
    home.project_entry.click()
    restored = home.project_window
    app.processEvents()
    assert restored.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == ("server", "s7")
    assert not restored.log_button.isChecked() and restored.logs.isHidden()
    assert abs(restored.navigation.sizes()[0] / sum(restored.navigation.sizes()) * 1000 - saved.layout.navigation) <= 2
    assert restored.server_view.currentIndex() == 1
    assert restored.connections.text() == "连接 0/2" and restored.listeners.text() == "监听 0/2"
    assert all(value.raw_value is None for store in restored.client._sample_stores.values() for value in store.samples.values())
    assert all(restored.client.workspace_device_status(item.id)[0] == ConnectionState.OFFLINE for item in saved.devices)
    assert all(pane.state == ListenerState.STOPPED for _, pane in restored.servers.panes())


def test_stale_selection_and_narrow_window_leave_tree_and_controls_available(workspace):
    app, home, path = workspace
    window = home.project_window
    assert window.close() is False
    wait_for(lambda: home.project_window is None)
    project = replace(home.workspace.project, layout=WorkspaceLayout(selection=("table", "deleted")))
    home.workspace.file.save(project, path)
    home.workspace.project = project
    home.project_entry.click()
    window = home.project_window
    window.resize(780, 620)
    app.processEvents()
    assert window.width() == 780
    assert window.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == ("device", "mb")
    QTest.keyClick(window.tree, Qt.Key.Key_Down)
    assert window.client.point_table_pane.table.id == "mb-table"
    assert window.context.toolTip().startswith("温度 · 同名设备")
    assert window.context.fontMetrics().horizontalAdvance(window.context.text()) <= window.context.width()
    window.nav_button.click()
    assert window.tree.isHidden()
    window.nav_button.click()
    assert not window.tree.isHidden()


def test_project_scene_controls_save_config_and_close_active_timer(workspace, monkeypatch):
    app, home, path = workspace
    window = home.project_window
    select(window, "server", "s7")
    pane = window._panes["s7"][2]
    errors = []

    def configure_rule(editor):
        editor.kind.setCurrentIndex(editor.kind.findData(RuleKind.TIMELINE.value))
        editor.actions.setPlainText("0 = 65536")
        editor.accept()
        errors.append(editor.error.text())
        editor.actions.setPlainText("0 = 800\n60000 = 850")
        editor.accept()
        return editor.result()

    def configure_scene(dialog):
        dialog.edit.click()
        return dialog.result()

    with monkeypatch.context() as patch:
        patch.setattr(RuleDialog, "exec", configure_rule)
        patch.setattr(SimulationDialog, "exec", configure_scene)
        pane.scene_button.click()
    assert errors and "范围" in errors[0]
    assert pane.dirty.text() == "有未保存修改"
    assert home._save_project()
    saved_rule = ProjectFile().open(path).s7.points[0].simulation
    assert saved_rule.kind == RuleKind.TIMELINE and saved_rule.actions[0].value == 800

    window.server_view.setCurrentIndex(1)
    pane.listen.click()
    wait_for(lambda: pane.simulation_update is not None)
    dialog = SimulationDialog(pane)
    dialog.show()
    assert dialog.start.isEnabled() and not dialog.edit.isEnabled()
    dialog.start.click()
    wait_for(lambda: pane.simulation_update.state == SimulationState.RUNNING)
    point = pane.configuration_plan().points[0]
    assert pane._memory.current(point) == 800
    dialog.pause.click()
    wait_for(lambda: pane.simulation_update.state == SimulationState.PAUSED)
    dialog.reset.click()
    wait_for(lambda: pane.simulation_update.state == SimulationState.READY)
    assert pane._memory.current(point) == point.initial
    dialog.start.click()
    wait_for(lambda: pane.simulation_update.state == SimulationState.RUNNING)
    task = pane.bridge.session._simulation_task
    dialog.close()
    dialog.deleteLater()

    def confirm_close():
        prompt = app.activeModalWidget()
        next(button for button in prompt.buttons() if prompt.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()

    QTimer.singleShot(0, confirm_close)
    window.close()
    wait_for(lambda: home.project_window is None)
    assert task.done() and not pane.bridge._thread.is_alive()
