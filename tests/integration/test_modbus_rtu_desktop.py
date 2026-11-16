from dataclasses import replace

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from all_protocol.adapters.modbus_rtu.client import ModbusRtuClient
from all_protocol.adapters.modbus_rtu.model import ModbusRtuTarget
from all_protocol.application.contracts import ProtocolRegistration
from all_protocol.project import Project, ProjectWorkspace, ProjectFile
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.modbus_rtu_form import ModbusRtuForm
from all_protocol.ui.role_window import RoleWindow


def test_rtu_forms_and_project_workspace_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    protocol = DesktopProtocol(ProtocolRegistration("modbus-rtu", "Modbus RTU", ModbusRtuClient, True), ModbusRtuForm)
    home = RoleWindow((protocol,), tmp_path / "local")
    home.workspace = ProjectWorkspace(Project("RTU desktop"))
    home._refresh_project()
    home._open_client()
    try:
        window = home.project_window
        client = window.client
        assert not client.configuration_profiles()
        form = client._form
        form.connection.serial_port.setCurrentText("com5")
        form.connection.baudrate.setValue(19200)
        fields = form.profile_fields()
        form.restore_profile(fields)
        assert form.connection_config()[1] == "COM5"
        assert isinstance(form.target(), ModbusRtuTarget)
        assert form.query.unit.maximum() == 247
        pane = window._panes["modbus-rtu"][2]
        pane.serial.serial_port.setCurrentText("COM6")
        pane.serial.baudrate.setValue(38400)
        assert "未保存" in pane.dirty.text()
        assert pane.listen.isVisibleTo(pane)  # serial controls must not hide listener actions
        pane.save.click()
        project = home._snapshot()
        assert project.devices[0].profile.endpoint == "COM5"
        assert project.rtu.serial_port == "COM6" and project.rtu.baudrate == 38400
        file = ProjectFile()
        path = tmp_path / "desktop.allprotocol"
        file.save(project, path)
        assert file.open(path) == project
    finally:
        home.join()
        app.processEvents()
        home.workspace = None
        home.close()
        home.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
