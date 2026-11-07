import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableView, QTabWidget, QVBoxLayout, QWidget,
)

from ..application.contracts import (
    ClientError, Command, ConnectionState, JournalEntry, OperationRejected, Point, ProtocolRegistration,
    ReadUpdate, SessionState, TableReadUpdate, Target, Value,
)
from ..storage import ConnectionProfile, LocalHistory, ProfileFields, VISIBLE_LOG_ROWS
from .io_bridge import IoBridge
from .widgets import add_field


class ProtocolForm(Protocol):
    connection: QWidget
    query: QWidget

    def connection_config(self) -> tuple[object, str]: ...
    def target(self) -> Target: ...
    def can_write(self) -> bool: ...
    def confirm_write(self, parent: QWidget, endpoint: str, target: Target) -> tuple[Value, ...] | None: ...


@runtime_checkable
class ProfileForm(Protocol):
    def profile_fields(self) -> ProfileFields: ...
    def restore_profile(self, fields: ProfileFields): ...


@dataclass(frozen=True)
class DesktopProtocol:
    protocol: ProtocolRegistration
    create_form: Callable[[], ProtocolForm]


class Rows[T](QAbstractTableModel):
    def __init__(self, headers: tuple[str, ...], format_row: Callable[[T], tuple[str, ...]],
                 right_aligned: tuple[int, ...] = (), monospace: tuple[int, ...] = ()):
        super().__init__()
        self.headers = headers
        self.format_row = format_row
        self.right_aligned = right_aligned
        self.monospace = monospace
        self.items: list[T] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.headers[section]
            if role == Qt.ItemDataRole.TextAlignmentRole and section in self.right_aligned:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index, role):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.format_row(self.items[index.row()])[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self.right_aligned:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.FontRole and index.column() in self.monospace:
            font = QFont("Consolas")
            font.setPixelSize(13)
            return font
        return None

    def reset(self, items: tuple[T, ...] = ()):
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def prepend(self, item: T):
        if len(self.items) == VISIBLE_LOG_ROWS:
            self.beginRemoveRows(QModelIndex(), VISIBLE_LOG_ROWS - 1, VISIBLE_LOG_ROWS - 1)
            self.items.pop()
            self.endRemoveRows()
        self.beginInsertRows(QModelIndex(), 0, 0)
        self.items.insert(0, item)
        self.endInsertRows()


def _table(model: Rows) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(27)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _heading(title: str) -> QLabel:
    label = QLabel(title)
    label.setObjectName("sectionTitle")
    return label


def _panel():
    widget = QFrame()
    widget.setObjectName("panel")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(14, 8, 14, 10)
    layout.setSpacing(7)
    return widget, layout


def _journal_row(entry: JournalEntry) -> tuple[str, ...]:
    return (entry.time.strftime("%m-%d %H:%M:%S"), entry.operation, entry.target,
            entry.function or "—", f"{entry.elapsed_ms} ms", entry.outcome)


class MainWindow(QMainWindow):
    def __init__(self, bridge: IoBridge, protocols: tuple[DesktopProtocol, ...],
                 history: LocalHistory | None = None, return_home: Callable[[], None] | None = None):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · 客户端")
        self.resize(1120, 800)
        self.setMinimumWidth(640)
        self._bridge = bridge
        self._history = history
        self._project_mode = bool(getattr(history, "project_mode", False))
        self._return_home = return_home
        self.before_close: Callable[[], bool] | None = None
        self._profiles: tuple[ConnectionProfile, ...] = ()
        self._protocols = protocols
        self._forms = tuple(item.create_form() for item in protocols)
        self._form_intervals = [1000] * len(protocols)
        self._state = SessionState()
        self._closing = False
        self._stopped = False
        self._stopping_poll = False
        self._last_read: ReadUpdate | None = None
        self.point_table_pane = None

        self.protocol = QComboBox()
        self.recent_connections = QComboBox()
        self.recent_connections.setAccessibleName("最近连接")
        if self._project_mode:
            self.recent_connections.setAccessibleName("项目设备")
        self.recent_connections.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.recent_connections.setMinimumContentsLength(17)
        self.recent_connections.setMaximumWidth(320)
        self.recent_connections.setToolTip("选择项目设备配置，再点击连接" if self._project_mode else "选择设备后填入连接配置，再点击连接")
        self.delete_recent_button = QPushButton("移除设备…" if self._project_mode else "删除记录…")
        self.delete_recent_button.setToolTip("移除项目设备及关联点表" if self._project_mode else "删除选中的最近连接记录")
        self.connection_forms = QStackedWidget()
        self.query_forms = QStackedWidget()
        for item, form in zip(protocols, self._forms, strict=True):
            self.protocol.addItem(item.protocol.label, item.protocol.key)
            self.connection_forms.addWidget(form.connection)
            self.query_forms.addWidget(form.query)
            form.query.changed.connect(self._query_changed)

        self.connection_button = QPushButton("连接")
        self.connection_button.setMinimumWidth(96)
        self.connection_status = QLabel()
        self.connection_status.setObjectName("connectionStatus")
        self.read_button = QPushButton("读取")
        self.read_button.setObjectName("primary")
        self.write_button = QPushButton("写入…")
        self.write_button.setObjectName("write")
        self.poll_button = QPushButton("启动轮询")
        self.interval = QSpinBox()
        self.interval.setRange(200, 30000)
        self.interval.setValue(1000)
        self.interval.setSuffix(" ms")
        self.interval.setToolTip("目标触发周期；慢响应不补发遗漏周期")
        self.interval.valueChanged.connect(self._interval_changed)
        self.result_context = QLabel()
        self.result_context.setObjectName("resultContext")
        self.feedback = QLabel()
        self.feedback.setObjectName("feedback")
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("packetDetail")
        self.detail.setAccessibleName("TX / RX 报文详情")
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(80)
        self.detail.setPlaceholderText("选择一条日志查看详情和 TX/RX 字节")

        self.results = Rows[Point](
            ("地址", "点位", "值", "十六进制"),
            lambda point: (point.address, point.label, str(point.value), point.hex_value),
            (2, 3),
        )
        self.journal = Rows[JournalEntry](
            ("时间", "操作", "目标", "功能码", "耗时", "结果"), _journal_row, (4,),
        )
        self.result_table = _table(self.results)
        self.result_table.setAccessibleName("读取结果")
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.result_table.horizontalHeader().setStretchLastSection(False)
        self.log_table = _table(self.journal)
        self.log_table.setAccessibleName("请求日志")
        self.log_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._build()
        if self._project_mode:
            for form in self._forms:
                form.connection.host.textChanged.connect(self._refresh)
                for field in form.connection.findChildren(QSpinBox):
                    field.valueChanged.connect(self._refresh)

        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.recent_connections.activated.connect(self._select_profile)
        self.delete_recent_button.clicked.connect(self._delete_profile)
        self.connection_button.clicked.connect(self._toggle_connection)
        self.read_button.clicked.connect(self._read)
        self.write_button.clicked.connect(self._write)
        self.poll_button.clicked.connect(self._toggle_poll)
        self.log_table.selectionModel().currentRowChanged.connect(self._show_detail)
        bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        bridge.stopped.connect(self._io_stopped, Qt.ConnectionType.QueuedConnection)
        self._load_history()
        self._refresh()

    @property
    def _form(self) -> ProtocolForm:
        return self._forms[self.protocol.currentIndex()]

    @property
    def _registration(self) -> ProtocolRegistration:
        return self._protocols[self.protocol.currentIndex()].protocol

    def _build(self):
        root = QWidget()
        root.setObjectName("workspace")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(18, 9, 18, 9)
        header.setSpacing(10)
        if self._return_home is not None:
            home = QPushButton("返回首页")
            home.clicked.connect(self._return_home)
            header.addWidget(home)
        icon = self.windowIcon()
        if not icon.isNull():
            mark = QLabel()
            mark.setPixmap(icon.pixmap(36, 36))
            header.addWidget(mark)
        title = QLabel("工业协议调试台")
        title.setObjectName("title")
        header.addWidget(title, 1)
        header.addWidget(self.connection_status)
        layout.addWidget(masthead)

        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(8)
        layout.addLayout(body, 1)

        connection, connection_layout = _panel()
        connection_header = QHBoxLayout()
        connection_header.addWidget(_heading("设备连接"), 1)
        if self._history is not None:
            recent_label = QLabel("项目设备" if self._project_mode else "最近连接")
            recent_label.setObjectName("fieldLabel")
            recent_label.setBuddy(self.recent_connections)
            connection_header.addWidget(recent_label)
            connection_header.addWidget(self.recent_connections)
            connection_header.addWidget(self.delete_recent_button)
        connection_layout.addLayout(connection_header)
        settings = QHBoxLayout()
        settings.setSpacing(10)
        add_field(settings, "协议", self.protocol)
        settings.addWidget(self.connection_forms, 1)
        settings.addWidget(self.connection_button, alignment=Qt.AlignmentFlag.AlignBottom)
        connection_layout.addLayout(settings)
        body.addWidget(connection)

        query, query_layout = _panel()
        query_layout.addWidget(_heading("点位操作"))
        query_layout.addWidget(self.query_forms)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.read_button)
        actions.addWidget(self.write_button)
        actions.addStretch(1)
        interval_label = QLabel("轮询间隔")
        interval_label.setObjectName("fieldLabel")
        interval_label.setBuddy(self.interval)
        actions.addWidget(interval_label)
        actions.addWidget(self.interval)
        actions.addWidget(self.poll_button)
        query_layout.addLayout(actions)
        query_layout.addWidget(self.feedback)

        results, result_layout = _panel()
        result_title = QHBoxLayout()
        result_title.addWidget(_heading("读取结果"), 1)
        result_title.addWidget(self.result_context)
        result_layout.addLayout(result_title)
        result_layout.addWidget(self.result_table)
        quick = QWidget()
        quick_layout = QVBoxLayout(quick)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.addWidget(query)
        quick_layout.addWidget(results, 1)
        content = quick
        if self._project_mode:
            from .point_table_pane import PointTablePane
            self.point_table_pane = PointTablePane(
                self._history.workspace, self._table_device,
                lambda: self._protocols[self.protocol.currentIndex()].create_form,
                self._save_table_device, self._read_table, self._write_point, lambda: self._registration.writable,
                self._poll_table, self._stop_all_polling,
            )
            self.point_views = QTabWidget()
            self.point_views.setAccessibleName("点位视图")
            self.point_views.addTab(quick, "快速读写")
            self.point_views.addTab(self.point_table_pane, "点表")
            content = self.point_views
        journal, journal_layout = _panel()
        journal_layout.addWidget(_heading("请求日志"))
        journal_layout.addWidget(self.log_table)
        journal_layout.addWidget(self.detail)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(content)
        split.addWidget(journal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(8)
        split.setSizes([350, 260])
        body.addWidget(split, 1)
        self.setCentralWidget(root)

    def _history_failure(self, message: str):
        logging.warning(message, exc_info=True)
        self.statusBar().showMessage(message, 8000)

    def _load_history(self):
        if self._history is None:
            return
        try:
            self.journal.reset(self._history.recent_entries())
        except OSError:
            self._history_failure("无法读取本地请求日志")
        try:
            self._show_profiles(self._history.profiles())
        except OSError:
            self._history_failure("无法读取最近连接")
        if self.recent_connections.count():
            self._select_profile(0)

    def _show_profiles(self, profiles: tuple[ConnectionProfile, ...]):
        self._profiles = profiles
        self.recent_connections.clear()
        for profile in profiles:
            index = self.protocol.findData(profile.protocol)
            if index != -1 and isinstance(self._forms[index], ProfileForm):
                label = f"{self.protocol.itemText(index)} · {profile.endpoint}"
                self.recent_connections.addItem(label, profile)
        self.recent_connections.setPlaceholderText(
            "选择设备" if self.recent_connections.count() else "暂无连接记录"
        )
        self.recent_connections.setCurrentIndex(-1)

    def _select_profile(self, index: int):
        profile = self.recent_connections.itemData(index)
        if profile is None:
            return
        if self._project_mode and self.before_close is not None:
            if not self.before_close():
                self.recent_connections.setCurrentIndex(-1)
                return
            key = profile.key
            profiles = self._history.profiles()
            self._show_profiles(profiles)
            index = next(i for i in range(self.recent_connections.count())
                         if self.recent_connections.itemData(i).key == key)
            profile = self.recent_connections.itemData(index)
        self.recent_connections.setCurrentIndex(index)
        protocol_index = self.protocol.findData(profile.protocol)
        form = self._forms[protocol_index]
        try:
            with QSignalBlocker(form.query):
                form.restore_profile(profile.fields)
        except (ClientError, KeyError, ValueError):
            self._history_failure("无法恢复此连接记录，请检查配置")
            self._refresh()
            return
        self.protocol.setCurrentIndex(protocol_index)
        self.interval.setValue(profile.interval_ms)
        self._query_changed()

    def _delete_profile(self):
        profile = self.recent_connections.currentData()
        if self._history is None or profile is None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("移除项目设备" if self._project_mode else "删除连接记录")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        question = "从当前项目移除此设备？" if self._project_mode else "删除这条最近连接记录？"
        if self._project_mode:
            count = self._history.related_table_count(profile)
            if count:
                question += f"\n同时删除关联的 {count} 张点表及全部点位。"
        dialog.setText(f"{question}\n{self.recent_connections.currentText()}")
        delete_button = dialog.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        confirmed = dialog.clickedButton() is delete_button
        dialog.deleteLater()
        if not confirmed:
            return
        try:
            profiles = self._history.forget(profile)
        except OSError:
            self._history_failure("无法删除最近连接记录")
            return
        self._show_profiles(profiles)
        if self._project_mode:
            # Do not silently add the removed device again on the next project save.
            self._forms[self.protocol.findData(profile.protocol)].connection.host.clear()
        self._refresh()

    def _remember_connection(self, endpoint: str):
        form = self._form
        if self._history is None or not isinstance(form, ProfileForm):
            return
        try:
            profile = ConnectionProfile(self._registration.key, endpoint, form.profile_fields(), self.interval.value())
            profiles = self._history.remember(profile)
        except (ClientError, OSError):
            self._history_failure("无法保存最近连接")
            return
        if profiles != self._profiles:
            self._show_profiles(profiles)
        key = self._registration.key, endpoint
        self.recent_connections.setCurrentIndex(next(
            (index for index in range(self.recent_connections.count())
             if self.recent_connections.itemData(index).key == key), -1,
        ))

    def configuration_profiles(self) -> tuple[ConnectionProfile, ...]:
        """Capture configured forms without issuing network operations."""
        profiles = []
        for index, (item, form) in enumerate(zip(self._protocols, self._forms, strict=True)):
            if not isinstance(form, ProfileForm):
                continue
            # Empty, untouched connection forms are not project devices.
            fields = getattr(form.connection, "host", None)
            if fields is not None and not fields.text().strip():
                continue
            _, endpoint = form.connection_config()
            profiles.append(ConnectionProfile(item.protocol.key, endpoint, form.profile_fields(), self._form_intervals[index]))
        return tuple(profiles)

    def _interval_changed(self, value):
        self._form_intervals[self.protocol.currentIndex()] = value

    def _protocol_changed(self, index):
        self.connection_forms.setCurrentIndex(index)
        self.query_forms.setCurrentIndex(index)
        if self._project_mode:
            self.interval.setValue(self._form_intervals[index])
        self._query_changed()

    def _query_changed(self):
        self._last_read = None
        self.results.reset()
        self.feedback.clear()
        self._refresh()

    def _toggle_connection(self):
        if self._state.connection == ConnectionState.ONLINE:
            self._state = replace(self._state, connection=ConnectionState.CLOSING)
            self._bridge.submit(self._bridge.session.disconnect())
        elif self._state.connection == ConnectionState.OFFLINE:
            try:
                config, endpoint = self._form.connection_config()
            except ClientError as error:
                self.feedback.setText(str(error))
                self._refresh()
                return
            self._query_changed()
            self._state = replace(self._state, connection=ConnectionState.CONNECTING, endpoint=endpoint)
            self._remember_connection(endpoint)
            self._bridge.submit(self._bridge.session.connect(self._registration, config, endpoint))
        self._refresh()

    def _read(self):
        self._execute(Command(self._form.target()))

    def _write(self):
        target = self._form.target()
        state = self._state
        values = self._form.confirm_write(self, state.endpoint, target)
        if values is not None and self._same_connection(state):
            self._execute(Command(target, values))

    def _table_device(self):
        try:
            _, endpoint = self._form.connection_config()
        except ClientError:
            return None
        return next((device for device in self._history.workspace.project.devices
                     if device.profile.key == (self._registration.key, endpoint)), None)

    def _save_table_device(self):
        try:
            _, endpoint = self._form.connection_config()
            profile = ConnectionProfile(self._registration.key, endpoint, self._form.profile_fields(), self.interval.value())
            self._show_profiles(self._history.remember(profile))
        except (ClientError, ValueError) as error:
            self.point_table_pane.feedback.setText(str(error))
            return
        self._refresh()

    def _read_table(self, table):
        device = self._table_device()
        if (device is None or device.id != table.device_id
                or self._state.connection != ConnectionState.ONLINE):
            return
        self._state = replace(self._state, busy=True, queued=self._state.queued + 1,
                              manual_tables=tuple(set(self._state.manual_tables) | {table.id}))
        self._refresh()
        commands = tuple((point.id, Command(point.target)) for point in table.points)
        self._bridge.submit(self._bridge.session.execute_table(table.id, commands))

    def _write_point(self, target):
        state = self._state
        if state.connection != ConnectionState.ONLINE or not self._registration.writable:
            return
        values = self._form.confirm_write(self, state.endpoint, target)
        if values is not None and self._same_connection(state):
            self._execute(Command(target, values))

    def _execute(self, command: Command, interval_ms: int | None = None):
        self.feedback.clear()
        self._remember_connection(self._state.endpoint)
        self._state = replace(self._state, busy=True, queued=self._state.queued + int(interval_ms is None),
                              polling=self._state.polling or interval_ms is not None)
        self._refresh()
        self._bridge.submit(self._bridge.session.execute(command, interval_ms))

    def _same_connection(self, state):
        return (self._state.connection == ConnectionState.ONLINE and self._state.generation == state.generation
                and self._state.endpoint == state.endpoint and not self._closing)

    def _poll_table(self, table, start):
        if start:
            self._state = replace(self._state, polling_groups=self._state.polling_groups + (table.id,))
            commands = tuple((point.id, Command(point.target)) for point in table.points)
            self._bridge.submit(self._bridge.session.start_table_polling(table.id, commands, table.interval_ms))
        else:
            self._bridge.submit(self._bridge.session.stop_table_polling(table.id))
        self._refresh()

    def _stop_all_polling(self):
        self._bridge.submit(self._bridge.session.stop_all_polling())

    def _toggle_poll(self):
        if self._state.polling:
            self._stopping_poll = True
            self._refresh()
            self._bridge.submit(self._bridge.session.stop_polling())
        else:
            self._execute(Command(self._form.target()), self.interval.value())

    def _handle_event(self, event):
        if isinstance(event, SessionState):
            if event.generation < self._state.generation:
                return
            self._state = event
            if not event.polling:
                self._stopping_poll = False
        elif isinstance(event, JournalEntry):
            if self._history is not None:
                try:
                    if self._history.append_entry(event):
                        self.journal.reset()
                        self.detail.clear()
                except OSError:
                    self._history_failure("无法保存本地请求日志")
            self.journal.prepend(event)
            if event.error or event.operation == "写入":
                self.feedback.setText(f"{event.outcome} · {event.detail}" if event.detail else event.outcome)
        elif isinstance(event, ReadUpdate) and event.generation == self._state.generation and event.command.target == self._form.target():
            if event.points is not None:
                self._last_read = event
                self.results.reset(event.points)
            elif self._last_read is not None:
                self._last_read = replace(self._last_read, points=None)
        elif isinstance(event, TableReadUpdate) and self.point_table_pane is not None:
            self.point_table_pane.handle_read(event)
        elif isinstance(event, OperationRejected) and event.generation == self._state.generation:
            self.feedback.setText(f"{event.operation}未执行 · {event.detail}")
            self.statusBar().showMessage(self.feedback.text(), 10000)
            if self.point_table_pane is not None:
                self.point_table_pane.feedback.setText(self.feedback.text())
        self._refresh()

    def _show_detail(self, index, _previous):
        if not index.isValid():
            self.detail.clear()
            return
        entry = self.journal.items[index.row()]
        index = self.protocol.findData(entry.protocol)
        protocol = self.protocol.itemText(index) if index != -1 else entry.protocol
        context = (entry.time.strftime("%Y-%m-%d %H:%M:%S"), protocol, entry.endpoint)
        lines = ["  ".join(filter(None, context)),
                 f"#{entry.request_id}  {entry.operation}  {entry.target}  {entry.outcome}"]
        if entry.operation_id is not None:
            lines.append(f"任务 #{entry.operation_id}")
        if entry.detail:
            lines.append(entry.detail)
        for sending, data in entry.packets:
            lines.append(f"{'TX' if sending else 'RX'}  {data.hex(' ').upper()}")
        self.detail.setPlainText("\n".join(lines))

    def _refresh(self):
        state = self._state
        online = state.connection == ConnectionState.ONLINE
        offline = state.connection == ConnectionState.OFFLINE
        idle = online and state.queued < state.queue_capacity and not self._closing
        self.connection_status.setText(
            f"{state.connection} · {state.endpoint}" if state.endpoint else state.connection
        )
        tone = "online" if online else "offline" if offline else "pending"
        if self.connection_status.property("tone") != tone:
            self.connection_status.setProperty("tone", tone)
            style = self.connection_status.style()
            style.unpolish(self.connection_status)
            style.polish(self.connection_status)
        self.feedback.setVisible(bool(self.feedback.text()))
        self.connection_button.setText("断开连接" if online else "连接")
        self.connection_button.setEnabled((online or offline) and not self._closing)
        self.protocol.setEnabled(offline and len(self._forms) > 1 and not self._closing)
        history_available = offline and not self._closing
        self.recent_connections.setEnabled(history_available and self.recent_connections.count() > 0)
        self.delete_recent_button.setEnabled(history_available and self.recent_connections.currentIndex() >= 0)
        self.connection_forms.setEnabled(offline and not self._closing)
        self.query_forms.setEnabled((offline or idle) and not self._closing)
        self.read_button.setEnabled(idle)
        self.write_button.setEnabled(idle and self._registration.writable and self._form.can_write())
        self.interval.setEnabled(idle or (offline and not self._closing))
        self.poll_button.setText("停止轮询" if state.polling else "启动轮询")
        self.poll_button.setEnabled(idle or (online and state.polling and not self._stopping_poll and not self._closing))
        if self._last_read is None:
            text = "选择数据区和地址后点击读取" if online else "先连接设备，再读取点位"
        else:
            entry = self._last_read.entry
            text = f"{entry.target} · {entry.time:%H:%M:%S}"
            if self._last_read.points is None or not online or self._last_read.generation != state.generation:
                text += " · 旧数据"
        self.result_context.setText(text)
        if self.point_table_pane is not None:
            self.point_table_pane.refresh(state, self._closing)

    def _io_stopped(self):
        self._stopped = True
        self.close()

    def closeEvent(self, event: QCloseEvent):
        if self._stopped:
            event.accept()
            return
        if not self._closing and self.before_close is not None and not self.before_close():
            event.ignore()
            return
        event.ignore()
        self._closing = True
        self._refresh()
        self._bridge.shutdown()
