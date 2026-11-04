from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..application.server_session import ServerSession
from ..storage import LocalHistory, ServerStorage
from .io_bridge import IoBridge
from .main_window import MainWindow, _heading, _panel
from .server_window import ServerWindow


class RoleWindow(QWidget):
    def __init__(self, protocols, data_dir: Path):
        super().__init__()
        self.setWindowTitle("工业协议调试台")
        self.setObjectName("workspace")
        self.resize(800, 400)
        self._protocols = protocols
        self._data_dir = data_dir
        self._bridge = None
        self._workbench = None

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
        cards.addWidget(self._card("服务端", "上位机  ──请求──→  本机", "监听连接，模拟多个 Modbus 从站。",
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
        self._bridge = IoBridge()
        self._open(MainWindow(self._bridge, self._protocols, LocalHistory(self._data_dir), self._return_home))

    def _open_server(self):
        self._bridge = IoBridge(ServerSession, ServerSession.stop)
        self._open(ServerWindow(self._bridge, ServerStorage(self._data_dir), self._return_home))

    def _open(self, workbench):
        self._workbench = workbench
        workbench.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        workbench.show()
        self.hide()

    def _return_home(self):
        self.show()

    def join(self):
        if self._bridge is not None:
            self._bridge.join()
