from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from all_protocol.adapters.modbus_tcp.server_model import ServerPlan
from all_protocol.adapters.s7.server_model import S7Memory
from all_protocol.application.contracts import ConnectionState, ProtocolRegistration
from all_protocol.application.server_session import ListenerState
from all_protocol.project import Project, ProjectFile, ProjectWorkspace
from all_protocol.storage import LocalHistory
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.s7_form import S7Form


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    def no_network(*_args):
        pytest.fail("项目操作不能创建网络客户端")

    protocols = (
        DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", no_network, True), ModbusTcpForm),
        DesktopProtocol(ProtocolRegistration("s7", "Siemens S7", no_network, True), S7Form),
    )
    home = RoleWindow(protocols, tmp_path / "local")
    errors = []
    monkeypatch.setattr(home, "_project_error", lambda error: errors.append(str(error)))
    home.show()
    yield app, home, errors
    server = home._workbenches.get("server")
    if server is not None:
        for index in range(2):
            pane = server[0].tabs.widget(index)
            pane._saved_plan = pane._plan()
    home.join()
    app.processEvents()
    home.workspace = None
    home.close()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def destroy_closed(app, home):
    for _ in range(30):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if not home._workbenches:
            return
        QTest.qWait(10)
    assert not home._workbenches


def choose_button(app, role):
    dialog = app.activeModalWidget()
    next(button for button in dialog.buttons() if dialog.buttonRole(button) == role).click()


def test_create_save_open_dual_protocol_project_without_network(desktop, monkeypatch, tmp_path):
    app, home, errors = desktop
    home._server_protocols = home._server_protocols[::-1]
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("工厂联调", True))
    home.project_buttons["新建项目"].click()
    assert home.workspace.project.name == "工厂联调"
    home._open_client()
    client = home._workbenches["client"][0]
    client._form.connection.host.setText("192.0.2.1")
    client._form.query.offset.setValue(10)
    client.interval.setValue(750)
    client.protocol.setCurrentIndex(1)
    client._form.connection.host.setText("192.0.2.2")
    client._form.connection.rack.setValue(1)
    client._form.connection.slot.setValue(3)
    client.interval.setValue(1500)
    home._open_server()
    server = home._workbenches["server"][0]
    server.tabs.widget(1).port.setValue(2502)
    s7 = server.tabs.widget(0)
    s7.port.setValue(2102)
    s7._points = (replace(s7._points[0], initial=123, name="反馈"),)
    s7._memory = S7Memory(s7._points)
    s7._memory.set_point(s7._points[0], 987)
    assert all(server.tabs.widget(index).state == ListenerState.STOPPED for index in range(2))
    assert s7.save.text() == "应用到项目"
    path = tmp_path / "项目.allprotocol.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    home.project_buttons["保存项目"].click()
    project = ProjectFile().open(path)
    assert {item.profile.protocol: item.profile.interval_ms for item in project.devices} == {"modbus-tcp": 750, "s7": 1500}
    assert project.modbus.port == 2502 and project.s7.port == 2102
    assert project.s7.points[0].initial == 123
    assert not LocalHistory(home._data_dir).profiles()
    assert not (home._data_dir / "modbus-tcp-server.json").exists()
    assert not (home._data_dir / "s7-server.json").exists()
    assert client._state.connection == ConnectionState.OFFLINE
    client.close()
    server.close()
    destroy_closed(app, home)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
    home.project_buttons["打开项目…"].click()
    assert home.workspace.project == project
    home._open_client()
    restored = home._workbenches["client"][0]
    assert restored.recent_connections.count() == 2
    assert restored._state.connection == ConnectionState.OFFLINE
    assert restored._form.connection.host.text() == "192.0.2.1"
    restored.recent_connections.activated.emit(1)
    assert restored._form.connection.host.text() == "192.0.2.2"
    assert restored.interval.value() == 1500
    home._open_server()
    restored_server = home._workbenches["server"][0]
    assert restored_server.tabs.widget(1).port.value() == 2502
    assert restored_server.tabs.widget(0)._points[0].initial == 123
    assert all(restored_server.tabs.widget(index).state == ListenerState.STOPPED for index in range(2))
    assert not errors


def test_unsaved_cancel_failed_open_and_switch_guard_preserve_project(desktop, monkeypatch, tmp_path):
    app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("原项目"))
    old = home.workspace
    QTimer.singleShot(0, lambda: choose_button(app, QMessageBox.ButtonRole.RejectRole))
    home._local_mode()
    assert home.workspace is old
    home._open_client()
    home._local_mode()
    assert home.workspace is old and "先关闭" in errors[-1]
    home._workbenches["client"][0].close()
    destroy_closed(app, home)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(invalid), ""))
    home._open_project()
    assert home.workspace is old
    QTimer.singleShot(0, lambda: choose_button(app, QMessageBox.ButtonRole.RejectRole))
    assert not home.close()
    assert home.isVisible()


def test_save_cancel_and_failure_leave_current_path_and_dirty_state(desktop, monkeypatch, tmp_path):
    _app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))
    assert not home._save_project()
    assert home.workspace.file.path is None and home.workspace.file.saved is None
    path = tmp_path / "good.json"
    home.workspace.file.save(home.workspace.project, path)
    home._open_client()
    client = home._workbenches["client"][0]
    client._form.connection.host.setText("bad host")
    assert not home._save_project()
    assert home.workspace.file.path == path
    assert home.workspace.file.saved == Project("项目")
    assert ProjectFile().open(path) == Project("项目")
    assert errors


def test_closed_client_edits_survive_and_removed_device_is_not_readded(desktop, monkeypatch, tmp_path):
    app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    home._open_client()
    client = home._workbenches["client"][0]
    client._form.connection.host.setText("192.0.2.1")
    client.close()
    destroy_closed(app, home)
    assert len(home.workspace.project.devices) == 1
    home._open_client()
    client = home._workbenches["client"][0]
    QTimer.singleShot(0, lambda: choose_button(app, QMessageBox.ButtonRole.DestructiveRole))
    client.delete_recent_button.click()
    for _ in range(50):
        app.processEvents()
        if not home.workspace.project.devices:
            break
        QTest.qWait(10)
    assert not home.workspace.project.devices
    path = tmp_path / "project.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    assert home._save_project()
    assert not ProjectFile().open(path).devices
    assert not errors


def test_save_as_selects_copy_and_preserves_source(desktop, monkeypatch, tmp_path):
    _app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    source = tmp_path / "source.json"
    home.workspace.file.save(home.workspace.project, source)
    before = source.read_bytes()
    home.workspace.project = replace(home.workspace.project, name="副本")
    target = tmp_path / "copy.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    assert home._save_project_as()
    assert home.workspace.file.path == target
    assert source.read_bytes() == before
    assert ProjectFile().open(target).name == "副本"
    assert not errors


def test_server_applied_plan_only_changes_project_memory(desktop):
    _app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    home._open_server()
    pane = home._workbenches["server"][0].tabs.widget(0)
    pane.port.setValue(3502)
    pane.save.click()
    assert home.workspace.project.modbus == ServerPlan(port=3502)
    assert home.workspace.file.saved is None
    assert not (home._data_dir / "modbus-tcp-server.json").exists()
    assert not errors


def test_switching_project_devices_retains_unconnected_edits(desktop, tmp_path):
    app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    home._open_client()
    client = home._workbenches["client"][0]
    client._form.connection.host.setText("192.0.2.1")
    assert home._capture_client()
    client._form.connection.host.setText("192.0.2.2")
    assert home._capture_client()
    client.close()
    destroy_closed(app, home)
    home._open_client()
    client = home._workbenches["client"][0]
    assert client._form.connection.host.text() == "192.0.2.2"
    client._form.query.offset.setValue(88)
    client.recent_connections.activated.emit(1)
    assert client._form.connection.host.text() == "192.0.2.1"
    saved_second = next(item for item in home.workspace.project.devices if item.profile.fields["host"] == "192.0.2.2")
    assert saved_second.profile.fields["offset"] == 88
    # An unchanged profile need not move to the front of the project list.
    # Remembering it must still leave the selector on the actual device.
    client._remember_connection("192.0.2.1:502")
    assert client.recent_connections.currentData().endpoint == "192.0.2.1:502"
    assert not errors


def test_close_project_save_and_discard_paths(desktop, monkeypatch, tmp_path):
    app, home, errors = desktop
    home.workspace = ProjectWorkspace(Project("项目"))
    home._open_client()
    home._workbenches["client"][0]._form.connection.host.setText("192.0.2.1")
    home._open_server()
    home._workbenches["server"][0].tabs.widget(0).port.setValue(4502)
    path = tmp_path / "saved.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    QTimer.singleShot(0, lambda: choose_button(app, QMessageBox.ButtonRole.AcceptRole))
    assert home.close()
    project = ProjectFile().open(path)
    assert project.modbus.port == 4502 and len(project.devices) == 1
    destroy_closed(app, home)
    home.show()
    home.workspace.project = replace(home.workspace.project, name="未保存的修改")
    QTimer.singleShot(0, lambda: choose_button(app, QMessageBox.ButtonRole.DestructiveRole))
    home._local_mode()
    assert home.workspace is None and ProjectFile().open(path).name == "项目"
    assert not errors
