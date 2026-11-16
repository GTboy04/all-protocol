from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget

from ..application.server_contracts import ListenerState, ServerRegistration
from ..application.server_session import ServerSession
from .io_bridge import IoBridge
from .server_io_bridge import ServerIoBridge
from .style import status_color


@dataclass(frozen=True, slots=True)
class DesktopServerProtocol:
    """The pane exposes listener state, endpoint labels, and plan/close hooks like ServerPane."""

    registration: ServerRegistration
    create_pane: Callable[[IoBridge, object], QWidget]
    create_storage: Callable[[Path], object]
    project_field: str | None = None


class ServerWorkbench(QMainWindow):
    overview_changed = Signal()
    stopped = Signal()

    def __init__(self, return_home, protocols_and_storages, *, embedded=False, workspace=None, protocols=None, storage_factory=None):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · 服务端")
        self.resize(1120, 800)
        self.setMinimumWidth(0 if embedded else 820)
        self._checking_close = False
        self._closing = False
        self._stopped = set()
        self.workspace = workspace
        self.available_protocols = protocols or {}
        self.storage_factory = storage_factory
        self._storages = []
        self._embedded = embedded
        self._bridges = []
        self._protocols = []
        self._build(return_home)
        theme = getattr(QApplication.instance(), "windows_theme", None)
        if theme is not None:
            theme.changed.connect(self._refresh_overview)
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
        if self._embedded:
            masthead.setParent(root)
            masthead.hide()
        else:
            layout.addWidget(masthead)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("serverProtocols")
        self.tabs.setAccessibleName("服务端协议")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setObjectName("serverProtocolTabs")
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setVisible(not self._embedded)
        layout.addWidget(self.tabs, 1)

    def _add(self, protocol, storage):
        registration = protocol.registration
        create_session = partial(ServerSession, registration=registration)
        bridge = ServerIoBridge(create_session, ServerSession.stop, storage)
        try:
            pane = protocol.create_pane(bridge, storage)
        except Exception:
            bridge.join()
            raise
        self._bridges.append(bridge)
        self._storages.append(storage)
        if getattr(storage, "simulator_id", None):
            protocol = replace(protocol, registration=replace(registration, key=storage.simulator_id, label=storage.simulator_name))
        self._protocols.append(protocol)
        self.tabs.addTab(pane, registration.label)
        pane.listener_changed.connect(self._refresh_overview)
        bridge.stopped.connect(partial(self._io_stopped, bridge), Qt.ConnectionType.QueuedConnection)
        return pane

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
            tone = "success" if active else "warning" if pane.state != ListenerState.STOPPED else "secondary"
            self.tabs.tabBar().setTabTextColor(index, status_color(self.tabs, tone))
        self.overview.setText(f"{listening} / {len(self._protocols)} 项监听中")
        tone = "online" if listening else "offline"
        if self.overview.property("tone") != tone:
            self.overview.setProperty("tone", tone)
            style = self.overview.style()
            style.unpolish(self.overview)
            style.polish(self.overview)
        self.overview_changed.emit()

    def panes(self):
        return tuple((protocol.registration, self.tabs.widget(index)) for index, protocol in enumerate(self._protocols))

    def mark_plans_saved(self):
        for _, pane in self.panes():
            pane._saved_plan = pane.configuration_plan()
            pane._load_error = False
            pane._refresh()

    def project_plans(self):
        if self.workspace is not None and self.workspace.project.simulators is not None:
            from ..project import ProjectSimulator
            return {"simulators": tuple(ProjectSimulator(protocol.registration.key, protocol.registration.label,
                        storage.simulator_protocol, self.tabs.widget(index).configuration_plan())
                        for index, (protocol, storage) in enumerate(zip(self._protocols, self._storages)))}
        return {protocol.project_field: self.tabs.widget(index).configuration_plan()
                for index, protocol in enumerate(self._protocols) if protocol.project_field is not None}

    def simulator_snapshot(self):
        from ..project import ProjectSimulator
        return tuple(ProjectSimulator(protocol.registration.key, protocol.registration.label, storage.simulator_protocol,
                                      self.tabs.widget(index).configuration_plan())
                     for index, (protocol, storage) in enumerate(zip(self._protocols, self._storages)))

    def add_simulator(self, item):
        if len(self._protocols) >= 64:
            raise ValueError("最多 64 个模拟服务")
        item.plan.validate()
        # Keep every unsaved form edit when promoting a legacy project.
        previous = self.workspace.project
        self.workspace.project = replace(previous, simulators=self.simulator_snapshot() + (item,))
        try:
            pane = self._add(self.available_protocols[item.protocol], self.storage_factory(item))
        except Exception:
            self.workspace.project = previous
            raise
        self.tabs.setCurrentWidget(pane)
        self._refresh_overview()
        return pane

    def rename_simulator(self, key, name):
        items = tuple(replace(item, name=name) if item.id == key else item for item in self.simulator_snapshot())
        self.workspace.project = replace(self.workspace.project, simulators=items)
        index = next(i for i, protocol in enumerate(self._protocols) if protocol.registration.key == key)
        protocol = self._protocols[index]
        self._protocols[index] = replace(protocol, registration=replace(protocol.registration, label=name))
        self._refresh_overview()

    def remove_simulator(self, key):
        index = next(i for i, protocol in enumerate(self._protocols) if protocol.registration.key == key)
        pane = self.tabs.widget(index)
        if pane.state != ListenerState.STOPPED:
            raise ValueError("请先停止该模拟设备的服务，再删除")
        items = tuple(item for item in self.simulator_snapshot() if item.id != key)
        bridge = self._bridges[index]
        bridge.join()
        self._stopped.discard(bridge)
        self.tabs.removeTab(index)
        self._protocols.pop(index)
        self._storages.pop(index)
        self._bridges.pop(index)
        self.workspace.project = replace(self.workspace.project, simulators=items)
        pane.journal_panel.hide()
        pane.journal_panel.setParent(pane)
        pane.deleteLater()
        self._refresh_overview()

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

    def shutdown_embedded(self):
        self._shutdown()
        if not self._bridges:
            self.stopped.emit()

    def join(self):
        self._shutdown()
        for bridge in self._bridges:
            bridge.join()

    def _io_stopped(self, bridge):
        if bridge not in self._bridges:
            return
        self._stopped.add(bridge)
        if len(self._stopped) == len(self._bridges):
            if not self._embedded:
                self.close()
            self.stopped.emit()

    def closeEvent(self, event):
        if len(self._stopped) == len(self._bridges):
            event.accept()
        else:
            event.ignore()
            self.request_close()
