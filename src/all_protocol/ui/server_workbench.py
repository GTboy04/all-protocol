from datetime import datetime
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget

from ..adapters.s7.server import S7Server, S7ServerRequest
from ..application.server_session import ListenerState, ServerSession
from ..storage import S7ServerStorage, ServerStorage
from .io_bridge import IoBridge
from .modbus_server_pane import ModbusServerPane
from .s7_server_pane import S7ServerPane


def _s7_lifecycle(message):
    return S7ServerRequest(datetime.now(), "", None, "", message)


class ServerWorkbench(QMainWindow):
    def __init__(self, data_dir, return_home, plan_storages=None):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · 服务端")
        self.resize(1120, 800)
        self.setMinimumWidth(820)
        self._checking_close = False
        self._closing = False
        self._stopped = 0
        self._bridges = []
        self._names = []
        self._build(return_home)
        try:
            storages = plan_storages or (ServerStorage(data_dir), S7ServerStorage(data_dir))
            self._add("Modbus TCP", ServerSession, ModbusServerPane, storages[0])
            self._add("Siemens S7", partial(ServerSession, create_server=S7Server, record_lifecycle=_s7_lifecycle),
                      S7ServerPane, storages[1])
        except Exception:
            self.join()
            raise
        self._refresh_overview()

    def _build(self, return_home):
        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(16, 10, 16, 10)
        home = QPushButton("返回首页")
        home.clicked.connect(return_home)
        header.addWidget(home)
        title = QLabel("服务端工作台")
        title.setObjectName("title")
        header.addWidget(title, 1)
        self.overview = QLabel()
        self.overview.setObjectName("connectionStatus")
        header.addWidget(self.overview)
        layout.addWidget(masthead)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("serverProtocols")
        self.tabs.setAccessibleName("服务端协议")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setObjectName("serverProtocolTabs")
        self.tabs.tabBar().setExpanding(False)
        layout.addWidget(self.tabs, 1)

    def _add(self, name, create_session, pane_type, storage):
        bridge = IoBridge(create_session, ServerSession.stop)
        self._bridges.append(bridge)
        pane = pane_type(bridge, storage)
        self._names.append(name)
        self.tabs.addTab(pane, name)
        pane.listener_changed.connect(self._refresh_overview)
        bridge.stopped.connect(self._io_stopped, Qt.ConnectionType.QueuedConnection)

    def _refresh_overview(self):
        listening = 0
        for index, name in enumerate(self._names):
            pane = self.tabs.widget(index)
            active = pane.state == ListenerState.LISTENING
            listening += int(active)
            endpoint = f" :{pane.port.value()}" if active else ""
            self.tabs.setTabText(index, f"{name}   {pane.state}{endpoint}")
            self.tabs.setTabToolTip(index, f"{name}   {pane.host.text()}:{pane.port.value()}   {pane.state}")
            color = "#154c47" if active else "#896b3d" if pane.state != ListenerState.STOPPED else "#405970"
            self.tabs.tabBar().setTabTextColor(index, QColor(color))
        self.overview.setText(f"{listening} / {len(self._names)} 项监听中")
        tone = "online" if listening else "offline"
        if self.overview.property("tone") != tone:
            self.overview.setProperty("tone", tone)
            style = self.overview.style()
            style.unpolish(self.overview)
            style.polish(self.overview)

    def request_close(self, *, confirm_listeners=True):
        if self._closing:
            return True
        if self._checking_close:
            return False
        self._checking_close = True
        try:
            panes = tuple(self.tabs.widget(index) for index in range(len(self._names)))
            if confirm_listeners and any(pane.state != ListenerState.STOPPED for pane in panes):
                if QMessageBox.question(self, "关闭服务端工作台", "关闭后将停止全部协议监听。继续关闭？") != QMessageBox.StandardButton.Yes:
                    return False
            for name, pane in zip(self._names, panes, strict=True):
                if not pane.confirm_close(self, name):
                    return False
            self._closing = True
            self.setEnabled(False)
            self._shutdown()
            return True
        finally:
            self._checking_close = False

    def _shutdown(self):
        for bridge in self._bridges:
            bridge.shutdown()

    def join(self):
        self._shutdown()
        for bridge in self._bridges:
            bridge.join()

    def _io_stopped(self):
        self._stopped += 1
        if self._stopped == len(self._bridges):
            self.close()

    def closeEvent(self, event):
        if self._stopped == len(self._bridges):
            event.accept()
        else:
            event.ignore()
            self.request_close()
