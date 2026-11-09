import asyncio
from dataclasses import replace
from datetime import datetime
import gc
from threading import get_ident
from time import monotonic

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Target
from all_protocol.application.contracts import (
    ClientError, Command, ConnectionState, DeviceEvent, ErrorKind, JournalEntry, Point,
    ProtocolRegistration, ReadUpdate, TableReadUpdate,
)
from all_protocol.application.sampling import Quality
from all_protocol.point_tables import PointDefinition, PointTable
from all_protocol.project import Project, ProjectDevice, ProjectFile, ProjectWorkspace
from all_protocol.storage import ConnectionProfile
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.s7_form import S7Form


def wait_for(condition):
    deadline = monotonic() + 3
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def choose(window, protocol):
    index = next(index for index in range(window.recent_connections.count())
                 if window.recent_connections.itemData(index).protocol == protocol)
    window.recent_connections.activated.emit(index)


def backend(window, operation):
    return asyncio.run_coroutine_threadsafe(operation, window._bridge._loop).result(timeout=2)


@pytest.fixture
def devices(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    threads, clients, read_failures, connect_failures, errors = [], [], set(), set(), []

    class Client:
        def __init__(self, config, events):
            self.config, self.events = config, events
            self.closed = 0
            clients.append(self)

        async def connect(self):
            threads.append(get_ident())
            if f"block:{self.config.host}" in connect_failures:
                await asyncio.Event().wait()
            if self.config.host in connect_failures:
                raise ClientError(ErrorKind.CONNECT, "模拟连接失败")

        async def close(self):
            threads.append(get_ident())
            self.closed += 1

        def function(self, command):
            return command.operation

        async def read(self, target):
            threads.append(get_ident())
            if self.config.host in read_failures:
                raise ClientError(ErrorKind.TIMEOUT, "模拟超时")
            value = 235 if self.config.host.endswith("1") else 125
            return tuple(Point(str(index), "value", value) for index in range(target.count))

        async def write(self, target, values):
            threads.append(get_ident())

    mb = ConnectionProfile("modbus-tcp", "192.0.2.1:502", {
        "host": "192.0.2.1", "port": 502, "timeout_ms": 1000, "unit_id": 1, "area": "HR",
        "offset": 0, "count": 1, "data_type": "UInt16", "order": "big",
    }, 200)
    s7 = ConnectionProfile("s7", "192.0.2.2:102 · Rack 0 / Slot 1", {
        "host": "192.0.2.2", "port": 102, "timeout_ms": 1000, "rack": 0, "slot": 1,
        "area": "DB", "db": 1, "offset": 0, "count": 1, "data_type": "Word", "bit": 0,
    }, 200)
    project = Project("多设备", (ProjectDevice("mb", "温控器", mb), ProjectDevice("s7", "PLC", s7)))
    project = project.with_table(PointTable("mb-table", "mb", "温度", (
        PointDefinition("mb-point", "温度", ModbusTarget(1, Area.HR, 0, 1), "℃", .1),), 200, 3000))
    project = project.with_table(PointTable("s7-table", "s7", "反馈", (
        PointDefinition("s7-point", "反馈", S7Target(S7Area.DB, 1, 0, S7Type.WORD)),), 200, 3000))
    workspace = ProjectWorkspace(project)
    protocols = (DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", Client, True), ModbusTcpForm),
                 DesktopProtocol(ProtocolRegistration("s7", "Siemens S7", Client, True), S7Form))
    home = RoleWindow(protocols, tmp_path / "local")
    home.workspace = workspace
    monkeypatch.setattr(home, "_project_error", lambda error: errors.append(str(error)))
    home.show()
    home._open_client()
    window = home.project_window.client
    yield app, home, window, workspace, clients, read_failures, connect_failures, threads, errors
    home.join()
    app.processEvents()
    home.workspace = None
    home.close()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    home.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def start_both(window):
    window.connect_all_button.click()
    wait_for(lambda: len(window._device_states) == 2 and all(
        state.connection == ConnectionState.ONLINE for state in window._device_states.values()))
    window.point_views.setCurrentIndex(1)
    choose(window, "modbus-tcp")
    window.point_table_pane.poll_button.click()
    choose(window, "s7")
    window.point_table_pane.poll_button.click()
    wait_for(lambda: window._sample_stores["mb"].samples["mb-table", "mb-point"].request_id is not None
             and window._sample_stores["s7"].samples["s7-table", "s7-point"].request_id is not None)


def test_switching_devices_keeps_background_polling_values_and_one_io_thread(devices):
    _app, _home, window, _workspace, clients, _read_failures, _connect_failures, threads, _errors = devices
    start_both(window)
    assert window._managed and window.recent_connections.isEnabled()
    assert not window.connection_forms.isEnabled()
    first = window._sample_stores["mb"].samples["mb-table", "mb-point"].request_id
    wait_for(lambda: window._sample_stores["mb"].samples["mb-table", "mb-point"].request_id > first)
    assert window._sample_stores["mb"].samples["mb-table", "mb-point"].raw_value == 235
    assert window._sample_stores["s7"].samples["s7-table", "s7-point"].raw_value == 125
    choose(window, "modbus-tcp")
    assert window._state.connection == ConnectionState.ONLINE
    assert window.point_table_pane.rows.data(window.point_table_pane.rows.index(0, 2), Qt.ItemDataRole.DisplayRole) == "235"
    window.read_button.click()
    wait_for(lambda: window._last_read is not None)
    choose(window, "s7")
    window.read_button.click()
    wait_for(lambda: window._last_read is not None)
    assert window.results.items[0].value == 125
    choose(window, "modbus-tcp")
    assert window.results.items[0].value == 235
    assert window.journal.items and all(entry.device_id == "mb" for entry in window.journal.items)
    assert all(entry.session_id for entry in window.journal.items)
    window.new_device_button.click()
    assert window._active_device_id is None and window.connection_forms.isEnabled()
    assert "已连接 2/2" in window.overview.text()
    choose(window, "s7")
    assert window._state.connection == ConnectionState.ONLINE and len(clients) == 2
    assert len(set(threads)) == 1 and threads[0] != get_ident()


def test_timeout_isolation_disconnect_all_and_old_read_generation(devices):
    _app, _home, window, _workspace, _clients, read_failures, _connect_failures, _threads, _errors = devices
    start_both(window)
    old_session = window._device_states["mb"].session_id
    old_value = window._sample_stores["mb"].samples["mb-table", "mb-point"]
    first_s7 = window._sample_stores["s7"].samples["s7-table", "s7-point"].request_id
    read_failures.add("192.0.2.1")
    wait_for(lambda: window._sample_stores["mb"].samples["mb-table", "mb-point"].quality == Quality.TIMEOUT)
    wait_for(lambda: window._sample_stores["s7"].samples["s7-table", "s7-point"].request_id > first_s7)
    sample = window._sample_stores["mb"].samples["mb-table", "mb-point"]
    assert sample.raw_value == 235 and sample.last_success_at == old_value.last_success_at
    assert window._sample_stores["s7"].samples["s7-table", "s7-point"].quality == Quality.GOOD
    window.disconnect_all_button.click()
    wait_for(lambda: all(state.connection == ConnectionState.OFFLINE for state in window._device_states.values()))
    assert all(not state.polling_groups for state in window._device_states.values())
    assert window._sample_stores["mb"].samples["mb-table", "mb-point"].quality == Quality.DISCONNECTED
    read_failures.clear()
    window.connect_all_button.click()
    wait_for(lambda: all(state.connection == ConnectionState.ONLINE for state in window._device_states.values()))
    assert window._device_states["mb"].session_id != old_session
    assert window._sample_stores["mb"].samples["mb-table", "mb-point"].quality == Quality.DISCONNECTED
    target = window._history.workspace.project.point_tables[0].points[0].target
    entry = JournalEntry(999, old_value.sampled_at, "读取", "old", "read", 0, "成功")
    update = TableReadUpdate("mb-table", "mb-point", ReadUpdate(Command(target), entry, (Point("0", "old", 999),), 1))
    window._handle_event(DeviceEvent("mb", old_session, 1, window._device_tokens["mb"], update))
    assert window._sample_stores["mb"].samples["mb-table", "mb-point"].raw_value == 235
    # The other device cannot route an identically numbered point/table into MB's store.
    window._handle_event(DeviceEvent("s7", window._device_states["s7"].session_id, 3, window._device_tokens["s7"],
                                    replace(update, update=replace(update.update, generation=3))))
    assert window._sample_stores["mb"].samples["mb-table", "mb-point"].raw_value == 235


def test_batch_failure_is_visible_and_manual_retry_is_independent(devices):
    _app, home, window, _workspace, clients, _read_failures, connect_failures, _threads, _errors = devices
    project = home.project_window
    connect_failures.add("192.0.2.2")
    window.connect_all_button.click()
    wait_for(lambda: "失败 1" in window.batch_status.text())
    assert window._device_states["mb"].connection == ConnectionState.ONLINE
    assert window._device_states["s7"].connection == ConnectionState.OFFLINE
    assert "模拟连接失败" in window.batch_status.toolTip()
    wait_for(lambda: project.error_button.text() == "异常 1")
    project.error_button.click()
    assert project.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == ("device", "s7")
    assert "模拟连接失败" in project.tree.currentItem().toolTip(0)
    connect_failures.clear()
    window.connection_button.click()
    wait_for(lambda: window._state.connection == ConnectionState.ONLINE)
    wait_for(lambda: project.error_button.text() == "异常 0")
    assert window._device_states["mb"].generation == 1 and window._state.generation == 2
    assert len(clients) == 3


def test_remove_running_device_waits_for_cleanup_and_does_not_stop_other_device(devices, monkeypatch, tmp_path):
    app, home, window, workspace, clients, _read_failures, _connect_failures, _threads, errors = devices
    start_both(window)
    choose(window, "modbus-tcp")
    old_token = window._device_tokens["mb"]
    first_s7 = window._sample_stores["s7"].samples["s7-table", "s7-point"].request_id

    def confirm():
        dialog = app.activeModalWidget()
        assert "先断开" in dialog.text()
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole).click()

    QTimer.singleShot(0, confirm)
    window.delete_recent_button.click()
    path = tmp_path / "project.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    assert not home._save_project() and "清理" in errors[-1]
    wait_for(lambda: len(workspace.project.devices) == 1)
    assert workspace.project.devices[0].id == "s7" and len(workspace.project.point_tables) == 1
    assert old_token in window._retired_tokens
    assert clients[0].closed == 1
    assert window._device_states["s7"].connection == ConnectionState.ONLINE
    wait_for(lambda: window._sample_stores["s7"].samples["s7-table", "s7-point"].request_id > first_s7)
    assert home._save_project()
    assert [device.id for device in ProjectFile().open(path).devices] == ["s7"]
    choose(window, "s7")
    window.connection_button.click()
    wait_for(lambda: window._state.connection == ConnectionState.OFFLINE)
    assert all(client.closed == 1 for client in clients)


def test_closing_workbench_drains_all_clients_before_thread_stops(devices):
    app, home, window, _workspace, clients, _read_failures, _connect_failures, _threads, _errors = devices
    start_both(window)
    bridge = window._bridge
    def discard():
        dialog = app.activeModalWidget()
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole).click()

    QTimer.singleShot(0, discard)
    home.project_window.close()
    wait_for(lambda: not bridge._thread.is_alive())
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(client.closed == 1 for client in clients)
    assert home.project_window is None


def test_rejected_connection_restores_actual_state_instead_of_sticking_in_connecting(devices):
    _app, _home, window, _workspace, clients, _read_failures, _connect_failures, _threads, _errors = devices

    async def set_disconnecting(value):
        window._bridge.session._disconnecting = value

    backend(window, set_disconnecting(True))
    window.connection_button.click()
    wait_for(lambda: "清理" in window.feedback.text() and window._state.connection == ConnectionState.OFFLINE)
    assert not clients and window.connection_button.isEnabled()
    backend(window, set_disconnecting(False))


def test_draft_matching_already_connected_device_binds_without_duplicate_connect(devices):
    _app, _home, window, _workspace, clients, _read_failures, _connect_failures, _threads, _errors = devices
    start_both(window)
    window.new_device_button.click()
    window._form.connection.host.setText("192.0.2.2")
    window.connection_button.click()
    assert window._active_device_id == "s7" and window._state.connection == ConnectionState.ONLINE
    assert len(clients) == 2


def test_cancel_one_connecting_device_keeps_other_connection_running(devices):
    _app, _home, window, _workspace, clients, _read_failures, connect_failures, _threads, _errors = devices
    connect_failures.add("block:192.0.2.2")
    window.connect_all_button.click()
    wait_for(lambda: window._device_states.get("s7") is not None
             and window._device_states["s7"].connection == ConnectionState.CONNECTING
             and window._device_states["mb"].connection == ConnectionState.ONLINE)
    choose(window, "s7")
    assert window.connection_button.text() == "取消连接" and window.connection_button.isEnabled()
    window.connection_button.click()
    wait_for(lambda: window._state.connection == ConnectionState.OFFLINE)
    assert window._device_states["mb"].connection == ConnectionState.ONLINE
    assert sum(client.closed for client in clients) == 1


def test_background_log_retention_evicts_old_rows_from_selected_device(devices, monkeypatch):
    _app, _home, window, _workspace, _clients, _read_failures, _connect_failures, _threads, _errors = devices
    monkeypatch.setattr(window._history, "append_entry", lambda _entry: False)
    window._append_journal(JournalEntry(1, datetime(2026, 11, 7), "读取", "mb", "read", 1, "成功", device_id="mb"))
    assert window.journal.rowCount() == 1
    for request_id in range(500):
        window._append_journal(JournalEntry(request_id, datetime(2026, 11, 7), "读取", "s7", "read", 1, "成功", device_id="s7"))
    assert len(window._all_entries) == 500 and window.journal.rowCount() == 0
    choose(window, "s7")
    assert window.journal.rowCount() == 500


def test_draft_cannot_overwrite_live_device_connection_parameters(devices):
    _app, home, window, workspace, clients, _read_failures, _connect_failures, _threads, errors = devices
    start_both(window)
    original = next(item.profile for item in workspace.project.devices if item.id == "s7")
    window.new_device_button.click()
    window._form.connection.host.setText("192.0.2.2")
    window._form.connection.timeout.setValue(9000)
    window.connection_button.click()
    assert window._active_device_id is None and window._state.connection == ConnectionState.OFFLINE
    assert "先断开" in window.feedback.text() and len(clients) == 2
    assert not home._capture_client() and "先断开" in errors[-1]
    assert next(item.profile for item in workspace.project.devices if item.id == "s7") == original
    window._form.connection.host.clear()
