import sys

from PySide6.QtWidgets import QApplication

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.application.contracts import ProtocolRegistration
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import STYLE, DesktopProtocol, MainWindow
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("工业协议调试台")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    protocols = (
        DesktopProtocol(ProtocolRegistration("modbus-tcp", "Modbus TCP", ModbusTcpClient, True), ModbusTcpForm),
    )
    bridge = IoBridge()
    try:
        window = MainWindow(bridge, protocols)
        window.show()
        return app.exec()
    finally:
        bridge.join()


if __name__ == "__main__":
    raise SystemExit(main())
