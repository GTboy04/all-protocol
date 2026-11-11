from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStackedWidget, QTabBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..application.contracts import ConnectionState
from ..application.server_contracts import ListenerState
from ..project import WorkspaceLayout
from ..reporting import ReportSnapshot
from .report_dialog import ReportDialog
from .style import status_color


class _ContextLabel(QLabel):
    def __init__(self, role="sectionTitle"):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setObjectName(role)

    def describe(self, name):
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setText(self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.describe(self.toolTip())


class ProjectWindow(QMainWindow):
    def __init__(self, workspace, client, servers, *, home, save, confirm_close):
        super().__init__()
        self.workspace, self.client, self.servers = workspace, client, servers
        self._confirm_close = confirm_close
        self._closing = self._checking_close = False
        self._pending_stops = {"client", "servers"}
        self._panes = {registration.key: (index, registration.label, pane)
                       for index, (registration, pane) in enumerate(servers.panes())}
        self._layout = workspace.project.layout or WorkspaceLayout()
        self._initial_layout = self._layout
        self._on_server = False
        self._items = {}
        self._structure = None
        self._errors = {}
        self._report = None
        self._opened_at = datetime.now().astimezone()
        self.setWindowTitle(f"{workspace.project.name} · 项目工作区")
        self.resize(1280, 840)
        self._build(home, save)
        theme = getattr(QApplication.instance(), "windows_theme", None)
        if theme is not None:
            theme.changed.connect(self._schedule_refresh)
        self._updates = QTimer(self, singleShot=True, interval=0)
        self._updates.timeout.connect(self._refresh_navigation)
        self.client.workspace_changed.connect(self._schedule_refresh)
        self.client.point_table_pane.workspace_changed.connect(self._schedule_refresh)
        self.client.point_views.currentChanged.connect(self._schedule_refresh)
        self.servers.overview_changed.connect(self._schedule_refresh)
        self.client.stopped.connect(self._client_stopped)
        self.servers.stopped.connect(self._servers_stopped)
        if not self._select(self._layout.selection):
            self._select(("device", self.client._active_device_id or ""))
        self._refresh_navigation()
        self._initial_layout = self._current_layout()

    def _build(self, home, save):
        root = QWidget(objectName="workspace")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame(objectName="masthead")
        toolbar = QHBoxLayout(header)
        toolbar.setContentsMargins(16, 12, 16, 12)
        toolbar.setSpacing(8)
        toolbar.addWidget(self._button("返回首页", home))
        save_button = self._button("保存项目", save)
        save_button.setShortcut("Ctrl+S")
        save_button.setToolTip("保存项目 (Ctrl+S)")
        save_button.setObjectName("primary")
        toolbar.addWidget(save_button)
        self.report_button = self._button("导出联调报告…", self._open_report)
        toolbar.addWidget(self.report_button)
        toolbar.addWidget(QLabel("项目工作区", objectName="title"))
        toolbar.addStretch()
        self.connections = QLabel(objectName="connectionStatus")
        self.listeners = QLabel(objectName="connectionStatus")
        toolbar.addWidget(self.connections)
        toolbar.addWidget(self.listeners)
        self.error_button = self._button("异常 0", self._locate_error)
        toolbar.addWidget(self.error_button)
        layout.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setObjectName("projectTree")
        self.tree.setAccessibleName("项目树")
        self.tree.setHeaderLabels(["项目导航"])
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setMinimumWidth(148)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.currentItemChanged.connect(self._tree_selected)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        context = QHBoxLayout()
        context.setContentsMargins(16, 12, 16, 4)
        context.setSpacing(8)
        self.nav_button = self._button("项目树", self._toggle_tree)
        self.nav_button.setCheckable(True)
        self.nav_button.setChecked(self._layout.navigation_visible)
        context.addWidget(self.nav_button)
        self.context = _ContextLabel()
        context.addWidget(self.context, 1)
        self.server_view = QTabBar()
        self.server_view.setAccessibleName("服务端视图")
        self.server_view.addTab("方案")
        self.server_view.addTab("当前值")
        self.server_view.setCurrentIndex(int(self._layout.server_values))
        self.server_view.currentChanged.connect(self._show_server_values)
        context.addWidget(self.server_view)
        body.addLayout(context)
        self.issue = _ContextLabel("warning")
        self.issue.setContentsMargins(12, 0, 12, 0)
        body.addWidget(self.issue)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.client)
        self.pages.addWidget(self.servers)
        # The existing protocol forms remain reachable when the window is narrow.
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.pages)
        body.addWidget(scroll, 1)
        self.navigation = QSplitter(Qt.Orientation.Horizontal)
        self.navigation.setChildrenCollapsible(False)
        self.navigation.addWidget(self.tree)
        self.navigation.addWidget(content)
        self.tree.setVisible(self.nav_button.isChecked())
        self.navigation.setStretchFactor(0, 1)
        self.navigation.setStretchFactor(1, 1)
        self.navigation.splitterMoved.connect(self._remember_navigation)

        journal = QWidget()
        journal_layout = QVBoxLayout(journal)
        journal_layout.setContentsMargins(16, 8, 16, 16)
        journal_layout.setSpacing(8)
        self.log_button = self._button("请求日志与报文", self._toggle_journal)
        self.log_button.setCheckable(True)
        self.log_button.setChecked(self._layout.journal_visible)
        journal_layout.addWidget(self.log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.logs = QStackedWidget()
        self.logs.setAccessibleName("当前视图的请求日志与报文")
        self.logs.addWidget(self.client.journal_panel)
        for _, _, pane in self._panes.values():
            pane.journal_heading.hide()
            self.logs.addWidget(pane.journal_panel)
        journal_layout.addWidget(self.logs, 1)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.vertical.setChildrenCollapsible(False)
        self.vertical.addWidget(self.navigation)
        self.vertical.addWidget(journal)
        self.vertical.setStretchFactor(0, 1)
        self.vertical.setStretchFactor(1, 1)
        self.vertical.splitterMoved.connect(self._remember_journal)
        layout.addWidget(self.vertical, 1)
        self._toggle_journal(self._layout.journal_visible)
        self._show_server_values(self.server_view.currentIndex())
        self._restore_sizes = True

    @staticmethod
    def _button(text, action):
        button = QPushButton(text)
        button.clicked.connect(action)
        return button

    def showEvent(self, event):
        super().showEvent(event)
        if self._restore_sizes:
            self._restore_sizes = False
            self._restore_navigation_size()
            self._toggle_journal(self.log_button.isChecked())

    def _restore_navigation_size(self):
        width = self.navigation.width() - self.navigation.handleWidth()
        size = width * self._layout.navigation // 1000
        self.navigation.setSizes([size, width - size])

    def _toggle_tree(self, visible):
        self.tree.setVisible(visible)
        if visible:
            self._restore_navigation_size()

    def _toggle_journal(self, visible):
        self.logs.setVisible(visible)
        self.log_button.setText("收起日志与报文" if visible else "展开日志与报文")
        self.vertical.handle(1).setVisible(visible)
        panel = self.vertical.widget(1)
        margins = panel.layout().contentsMargins()
        collapsed = self.log_button.sizeHint().height() + margins.top() + margins.bottom()
        panel.setMaximumHeight(16777215 if visible else collapsed)
        if visible:
            self._restore_journal_size()
        else:
            self.vertical.setSizes([self.vertical.height() - collapsed, collapsed])

    def _restore_journal_size(self):
        height = self.vertical.height() - self.vertical.handleWidth()
        size = height * self._layout.journal // 1000
        self.vertical.setSizes([height - size, size])

    @staticmethod
    def _ratio(sizes, index):
        return max(100, min(850, round(1000 * sizes[index] / max(1, sum(sizes)))))

    def _remember_navigation(self, *_):
        self._layout = replace(self._layout, navigation=self._ratio(self.navigation.sizes(), 0))

    def _remember_journal(self, *_):
        if self.log_button.isChecked():
            self._layout = replace(self._layout, journal=self._ratio(self.vertical.sizes(), 1))

    def _current_layout(self):
        return replace(self._layout, journal_visible=self.log_button.isChecked(),
                       selection=self._selection(), server_values=bool(self.server_view.currentIndex()),
                       navigation_visible=self.nav_button.isChecked())

    def layout_snapshot(self):
        layout = self._current_layout()
        return self.workspace.project.layout if layout == self._initial_layout else layout

    def mark_layout_saved(self):
        self._initial_layout = self._current_layout()

    def _selection(self):
        if self._on_server:
            return ("server", next(key for key, (index, _, _) in self._panes.items()
                                   if index == self.servers.tabs.currentIndex()))
        device_id = self.client._active_device_id or ""
        if self.client.point_views.currentIndex() == 1:
            table = self.client.point_table_pane.table
            return ("table", table.id) if table is not None else ("points", device_id)
        return ("device", device_id)

    def show_clients(self):
        self._on_server = False
        self._refresh_navigation()

    def show_servers(self):
        if self._panes and not self._on_server:
            key = next(key for key, (index, _, _) in self._panes.items() if index == self.servers.tabs.currentIndex())
            self._select(("server", key))

    def _select(self, key):
        kind, value = key
        if kind == "server":
            server = self._panes.get(value)
            if server is None:
                return False
            self._on_server = True
            self.servers.tabs.setCurrentIndex(server[0])
        elif kind in ("device", "points", "table"):
            if kind == "table":
                table = next((item for item in self.workspace.project.point_tables if item.id == value), None)
                if table is None:
                    return False
                device_id, table_id = table.device_id, table.id
            else:
                device_id, table_id = value or None, None
            if not self.client.select_workspace_device(device_id, points=kind != "device", table_id=table_id):
                return False
            self._on_server = False
        elif kind == "clients":
            self._on_server = False
        else:
            return False
        self._refresh_navigation()
        return True

    def _show_server_values(self, index):
        for _, _, pane in self._panes.values():
            pane.show_workspace_values(bool(index))

    def _tree_selected(self, current, _previous):
        if current is not None and not self._select(current.data(0, Qt.ItemDataRole.UserRole)):
            self._refresh_navigation()

    def _schedule_refresh(self, *_):
        if not self._closing and not self._updates.isActive():
            self._updates.start()

    def _rebuild_tree(self, devices, tables):
        expanded = {key for key, item in self._items.items() if item.isExpanded()}
        first = not self._items
        self.tree.clear()
        self._items.clear()

        def add(parent, key, name, selectable=True):
            item = QTreeWidgetItem(parent, [name])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setToolTip(0, name)
            if not selectable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setExpanded(first or key in expanded or key[0] == "device")
            self._items[key] = item
            return item

        clients = add(self.tree, ("clients", ""), "设备")
        if not devices:
            add(clients, ("empty", ""), "暂无设备", selectable=False)
        for device_id, name in devices:
            add(clients, ("device", device_id), name)
        for device_id, table_id, name in tables:
            add(self._items["device", device_id], ("table", table_id), name)
        if self._panes:
            servers = add(self.tree, ("servers", ""), "模拟服务", selectable=False)
            for key, (_, label, _) in self._panes.items():
                add(servers, ("server", key), label)

    def _refresh_navigation(self):
        project = self.workspace.project
        devices = tuple((device.id, device.name) for device in project.devices)
        tables = tuple((table.device_id, table.id, table.name) for table in project.point_tables)
        selection = self._selection()
        kind, value = selection
        current = (("device", value) if kind == "points" else selection) if value else ("clients", "")
        online = listening = 0
        errors = {}
        with QSignalBlocker(self.tree):
            if (devices, tables) != self._structure:
                self._rebuild_tree(devices, tables)
                self._structure = devices, tables
            for device in project.devices:
                key = ("device", device.id)
                state, error = self.client.workspace_device_status(device.id)
                online += int(state == ConnectionState.ONLINE)
                self._set_status(key, device.name, state, device.profile.endpoint, error)
                if error:
                    errors[key] = (device.name, error)
            for protocol, (_, label, pane) in self._panes.items():
                key = ("server", protocol)
                listening += int(pane.state == ListenerState.LISTENING)
                self._set_status(key, label, pane.state, pane.endpoint if pane.state == ListenerState.LISTENING
                                 else pane.configured_endpoint, pane.listener_error)
                if pane.listener_error:
                    errors[key] = (label, pane.listener_error)
            item = self._items.get(current, self._items["clients", ""])
            if self.tree.currentItem() != item:
                self.tree.setCurrentItem(item)
        self._count(self.connections, "连接", online, len(devices))
        self._count(self.listeners, "监听", listening, len(self._panes))
        self._errors = errors
        self.error_button.setEnabled(bool(errors))
        self.error_button.setText(f"异常 {len(errors)}")
        self.error_button.setToolTip("\n".join(f"{name}：{error}" for name, error in errors.values()))
        self.pages.setCurrentWidget(self.servers if self._on_server else self.client)
        self.server_view.setVisible(self._on_server)
        self.logs.setCurrentWidget(self._panes[value][2].journal_panel if self._on_server else self.client.journal_panel)
        names = dict(devices)
        if kind == "table":
            table = self.client.point_table_pane.table
            context = f"{table.name} · {names.get(table.device_id, '')}"
            error_key = ("device", table.device_id)
        else:
            context = self._panes[value][1] if self._on_server else names.get(value, "新设备配置")
            error_key = ("server" if self._on_server else "device", value)
        self.context.describe(context)
        _, detail = errors.get(error_key, ("", ""))
        self.issue.describe(detail)
        self.issue.setVisible(bool(detail))

    @staticmethod
    def _count(label, name, active, total):
        label.setText(f"{name} {active}/{total}")
        tone = "online" if active else "offline"
        if label.property("tone") != tone:
            label.setProperty("tone", tone)
            label.style().unpolish(label)
            label.style().polish(label)

    def _set_status(self, key, name, state, endpoint, error):
        item = self._items[key]
        item.setText(0, f"[{'异常' if error else state}]  {name}")
        item.setToolTip(0, "\n".join(filter(None, (name, endpoint, str(state), error))))
        item.setForeground(0, status_color(self.tree, "warning" if error else "success" if state in
                                          (ConnectionState.ONLINE, ListenerState.LISTENING) else "secondary"))

    def _locate_error(self):
        keys = tuple(self._errors)
        if not keys:
            return
        current = self._selection()
        key = keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]
        if self._select(key):
            if not self.nav_button.isChecked():
                self.nav_button.click()
            item = self._items[key]
            item.parent().setExpanded(True)
            self.tree.scrollToItem(item)
            self.tree.setFocus()

    def _open_report(self):
        if self._report is None:
            self._report = ReportDialog(self, self._capture_report)
            self._report.finished.connect(self._report_closed)
            self._report.open()

    def _capture_report(self, scope, frozen_at):
        return ReportSnapshot.capture(self.workspace.project, scope, self.client.report_log(),
                                      tuple((protocol, pane.report_log()) for protocol, (_, _, pane) in self._panes.items()),
                                      self._opened_at, frozen_at)

    def _report_closed(self):
        self._report = None

    def has_running_io(self):
        return (any(self.client.workspace_device_status(device.id)[0] != ConnectionState.OFFLINE
                    for device in self.workspace.project.devices)
                or any(pane.state != ListenerState.STOPPED for _, _, pane in self._panes.values()))

    def request_close(self):
        if self._closing:
            return True
        if self._checking_close:
            return False
        self._checking_close = True
        try:
            if not self._confirm_close(self):
                return False
            self._closing = True
            self.setEnabled(False)
            if self._report is not None:
                self._report.reject()
            self._updates.stop()
            self.client.shutdown_embedded()
            self.servers.shutdown_embedded()
            return True
        finally:
            self._checking_close = False

    def _client_stopped(self):
        self._io_stopped("client")

    def _servers_stopped(self):
        self._io_stopped("servers")

    def _io_stopped(self, side):
        self._pending_stops.discard(side)
        if not self._pending_stops:
            self.close()

    def join(self):
        if self._report is not None:
            self._report.reject()
        self.client._bridge.join()
        self.servers.join()

    def closeEvent(self, event):
        if not self._pending_stops:
            event.accept()
            return
        event.ignore()
        self.request_close()
