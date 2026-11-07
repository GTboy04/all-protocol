import sys
from importlib.resources import as_file, files
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.s7.client import S7Client
from all_protocol.application.contracts import ProtocolRegistration
from all_protocol.ui.main_window import DesktopProtocol
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.s7_form import S7Form
from all_protocol.ui.style import STYLE


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("工业协议调试台")
    icon = files("all_protocol").joinpath("assets", "app-icon.ico")
    with as_file(icon) as icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    protocols = (
        DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", ModbusTcpClient, True), ModbusTcpForm),
        DesktopProtocol(ProtocolRegistration("s7", "Siemens S7（ISO-on-TCP）", S7Client, True), S7Form),
    )
    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
    window = RoleWindow(protocols, data_dir / "AllProtocol")
    try:
        window.show()
        return app.exec()
    finally:
        window.join()


if __name__ == "__main__":
    raise SystemExit(main())
