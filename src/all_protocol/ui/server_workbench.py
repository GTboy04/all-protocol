from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget

from ..application.server_contracts import ListenerState, ServerRegistration
from ..application.server_session import ServerSession
from .io_bridge import IoBridge


@dataclass(frozen=True, slots=True)
class DesktopServerProtocol:
    """The pane exposes listener state, endpoint labels, and plan/close hooks like ServerPane."""

    registration: ServerRegistration
    create_pane: Callable[[IoBridge, object], QWidget]
    create_storage: Callable[[Path], object]
    project_field: str | None = None


class ServerWorkbench(QMainWindow):
    def __init__(self, return_home, protocols_and_storages):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · 服务端")
        self.resize(1120, 800)
        self.setMinimumWidth(820)
        self._checking_close = False
        self._closing = False
        self._stopped = 0
        self._bridges = []
        self._protocols = []
        self._build(return_home)
        try:
            for protocol, storage in protocols_and_storages:
                self._add(protocol, storage)
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

    def _add(self, protocol, storage):
        registration = protocol.registration
        create_session = partial(ServerSession, registration=registration)
        bridge = IoBridge(create_session, ServerSession.stop)
        self._bridges.append(bridge)
        pane = protocol.create_pane(bridge, storage)
        self._protocols.append(protocol)
        self.tabs.addTab(pane, registration.label)
        pane.listener_changed.connect(self._refresh_overview)
        bridge.stopped.connect(self._io_stopped, Qt.ConnectionType.QueuedConnection)

    def _refresh_overview(self):
        listening = 0
        for index, protocol in enumerate(self._protocols):
            name = protocol.registration.label
            pane = self.tabs.widget(index)
            active = pane.state == ListenerState.LISTENING
            listening += int(active)
            endpoint = pane.endpoint if active else pane.configured_endpoint
            self.tabs.setTabText(index, f"{name}   {pane.state}" + (f" {endpoint}" if active else ""))
            self.tabs.setTabToolTip(index, f"{name}   {endpoint}   {pane.state}")
            color = "#154c47" if active else "#896b3d" if pane.state != ListenerState.STOPPED else "#405970"
            self.tabs.tabBar().setTabTextColor(index, QColor(color))
        self.overview.setText(f"{listening} / {len(self._protocols)} 项监听中")
        tone = "online" if listening else "offline"
        if self.overview.property("tone") != tone:
            self.overview.setProperty("tone", tone)
            style = self.overview.style()
            style.unpolish(self.overview)
            style.polish(self.overview)

    def project_plans(self):
        return {protocol.project_field: self.tabs.widget(index).configuration_plan()
                for index, protocol in enumerate(self._protocols) if protocol.project_field is not None}

    def request_close(self, *, confirm_listeners=True):
        if self._closing:
            return True
        if self._checking_close:
            return False
        self._checking_close = True
        try:
            panes = tuple(self.tabs.widget(index) for index in range(len(self._protocols)))
            if confirm_listeners and any(pane.state != ListenerState.STOPPED for pane in panes):
                if QMessageBox.question(self, "关闭服务端工作台", "关闭后将停止全部协议监听。继续关闭？") != QMessageBox.StandardButton.Yes:
                    return False
            for protocol, pane in zip(self._protocols, panes, strict=True):
                if not pane.confirm_close(self, protocol.registration.label):
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
