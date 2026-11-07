from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..storage import LocalHistory
from .io_bridge import IoBridge
from .main_window import MainWindow, _heading, _panel
from .server_workbench import ServerWorkbench


class RoleWindow(QWidget):
    def __init__(self, protocols, data_dir: Path):
        super().__init__()
        self.setWindowTitle("工业协议调试台")
        self.setObjectName("workspace")
        self.resize(800, 400)
        self._protocols = protocols
        self._data_dir = data_dir
        self._workbenches = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(18, 12, 18, 12)
        title = QLabel("工业协议调试台")
        title.setObjectName("title")
        header.addWidget(title)
        layout.addWidget(masthead)

        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 24)
        body.setSpacing(14)
        body.addWidget(_heading("选择调试模式"))
        cards = QHBoxLayout()
        cards.setSpacing(14)
        cards.addWidget(self._card("客户端", "本机  ──请求──→  设备", "连接设备，读取或写入点位。",
                                   self._open_client), 1)
        cards.addWidget(self._card("服务端", "上位机  ──请求──→  本机", "同时监听 Modbus TCP 和 Siemens S7。",
                                   self._open_server), 1)
        body.addLayout(cards, 1)
        layout.addLayout(body, 1)

    def _card(self, role, direction, description, action):
        card, layout = _panel()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(_heading(role))
        arrow = QLabel(direction)
        arrow.setObjectName("permissions")
        layout.addWidget(arrow)
        layout.addWidget(QLabel(description))
        layout.addStretch()
        button = QPushButton(f"进入{role}")
        button.setObjectName("primary")
        button.clicked.connect(action)
        layout.addWidget(button)
        return card

    def _open_client(self):
        def create():
            bridge = IoBridge()
            try:
                return MainWindow(bridge, self._protocols, LocalHistory(self._data_dir), self._return_home), bridge
            except Exception:
                bridge.join()
                raise

        self._open("client", create)

    def _open_server(self):
        def create():
            workbench = ServerWorkbench(self._data_dir, self._return_home)
            return workbench, workbench

        self._open("server", create)

    def _open(self, role, create):
        if role in self._workbenches:
            workbench, _ = self._workbenches[role]
        else:
            workbench, owner = create()
            self._workbenches[role] = workbench, owner
            workbench.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            workbench.destroyed.connect(lambda: self._workbenches.pop(role))
        self._activate(workbench)

    @staticmethod
    def _activate(workbench):
        workbench.show()
        if workbench.isMinimized():
            workbench.setWindowState(workbench.windowState() & ~Qt.WindowState.WindowMinimized)
        workbench.raise_()
        workbench.activateWindow()

    def _return_home(self):
        self._activate(self)

    def join(self):
        for _, owner in tuple(self._workbenches.values()):
            owner.join()

    def closeEvent(self, event):
        server = self._workbenches.get("server")
        if server is not None and not server[0].request_close(confirm_listeners=False):
            event.ignore()
            return
        client = self._workbenches.get("client")
        if client is not None:
            client[0].close()
        event.accept()
