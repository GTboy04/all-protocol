import asyncio
import socket
from time import monotonic

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from all_protocol.adapters.opcua.client import OpcUaClient
from all_protocol.adapters.opcua.model import UaTarget, UaType
from all_protocol.adapters.opcua.server_model import UaServerPoint, SIMULATOR_NAMESPACE
from all_protocol.application.contracts import ConnectionState, ProtocolRegistration
from all_protocol.application.server_contracts import ListenerState
from all_protocol.point_tables import PointDefinition, PointTable
from all_protocol.project import Project, ProjectFile, ProjectWorkspace
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.opcua_form import OpcUaForm
from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.write_dialog import WriteDialog


def wait_for(condition):
    deadline = monotonic() + 6
    while not condition() and monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


def test_ua_desktop_browse_subscribe_server_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    old_font = app.font()
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    if font_id >= 0:
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 9))
    protocol = DesktopProtocol(ProtocolRegistration("opcua", "OPC UA", OpcUaClient, True), OpcUaForm)
    home = RoleWindow((protocol,), tmp_path / "local")
    home.workspace = ProjectWorkspace(Project("OPC UA desktop"))
    home._refresh_project()
    home._open_client()
    try:
        window, client = home.project_window, home.project_window.client
        assert not client.configuration_profiles()
        pane = window._panes["opcua"][2]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        target = UaTarget(SIMULATOR_NAMESPACE, "s", "Counter", UaType.Int32)
        pane.port.setValue(port)
        pane.security.setCurrentIndex(pane.security.findData("None/None"))
        pane._points = (UaServerPoint(target, 42, "Counter"),)
        pane.points.reset(pane._points)
        pane.save.click()
        pane.listen.click()
        wait_for(lambda: pane.state == ListenerState.LISTENING)
        form = client._form
        form.connection.host.setText(pane.configured_endpoint)
        form.connection.security.setCurrentIndex(form.connection.security.findData("None/None"))
        form.query.set_target(UaTarget(SIMULATOR_NAMESPACE, "s", "AllProtocol", UaType.Byte))
        client.connection_button.click()
        wait_for(lambda: client._state.connection == ConnectionState.ONLINE)
        form.query.browse.click()
        wait_for(lambda: form.query.nodes.topLevelItemCount() == 1)
        form.query.nodes.itemDoubleClicked.emit(form.query.nodes.topLevelItem(0), 0)
        assert form.target() == target
        client.read_button.click()
        wait_for(lambda: bool(client.results.items))
        assert client.results.items[0].value == 42
        assert "Good" in client._result_row(client.results.items[0])[-1]
        device_id = client._active_device_id
        table = PointTable("ua-table", device_id, "UA values", (PointDefinition("ua-point", "Counter", target),), 200)
        home.workspace.project = home.workspace.project.with_table(table)
        client._refresh()
        points = client.point_table_pane
        assert points.table == table
        points.subscribe_button.click()
        wait_for(lambda: table.id in client._state.subscription_groups)
        wait_for(lambda: points.samples.samples[(table.id, "ua-point")].raw_value == 42)
        asyncio.run_coroutine_threadsafe(pane.bridge.session.write_current(target, 99), pane.bridge._loop).result(2)
        wait_for(lambda: points.samples.samples[(table.id, "ua-point")].raw_value == 99)
        assert "采样" in points.samples.samples[(table.id, "ua-point")].detail
        points.stop_all_button.click()
        wait_for(lambda: not client._state.subscription_groups)
        project = home._snapshot()
        file = ProjectFile()
        path = tmp_path / "ua-desktop.allprotocol"
        file.save(project, path)
        assert file.open(path) == project
        window.resize(1400, 950)
        app.processEvents()
        window.grab().save(str(tmp_path / "opcua-desktop.png"))
    finally:
        home.join()
        app.processEvents()
        home.workspace = None
        home.close()
        home.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.setFont(old_font)


def test_ua_string_write_dialog_preserves_text(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    target = UaTarget(SIMULATOR_NAMESPACE, "s", "Text", UaType.String)
    dialog = WriteDialog(None, "opc.tcp://127.0.0.1:4840/", target)
    try:
        for text in (" space, comma\nand 中文 ", ""):
            dialog.input.setPlainText(text)
            dialog.accept()
            assert dialog.command.values == (text,)
    finally:
        dialog.deleteLater()
        app.processEvents()
