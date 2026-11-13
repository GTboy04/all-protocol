import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableView, QTabWidget, QVBoxLayout, QWidget,
)

from ..application.contracts import (
    BatchResult, ClientError, Command, ConnectionState, DeviceEvent, DeviceRemoved, JournalEntry, OperationRejected, Point, ProtocolRegistration,
    ReadUpdate, SessionState, TableReadUpdate, Target, WriteStatus,
)
from ..application.diagnostics import DiagnosticRow, verification_summary, write_evidence_lines
from ..application.binary import binary_text, bit_pattern, target_kind
from ..application.journal import LogRetention, RetainedLog
from ..point_tables import PointDefinition
from ..application.session_manager import SessionManager
from ..application.sampling import SampleStore
from ..project import validate_profile
from ..storage import ConnectionProfile, LocalHistory, ProfileFields, VISIBLE_LOG_ROWS
from .io_bridge import IoBridge
from .widgets import add_field
from .journal_view import JournalView
from .bit_inspector import BitReading, show_bit_inspector


class ProtocolForm(Protocol):
    connection: QWidget
    query: QWidget

    def connection_config(self) -> tuple[object, str]: ...
    def target(self) -> Target: ...
    def can_write(self) -> bool: ...
    def confirm_write(self, parent: QWidget, endpoint: str, target: Target,
                      point: PointDefinition | None = None) -> Command | None: ...


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
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
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

    def discard_oldest(self, item: T):
        if self.items and self.items[-1] == item:
            row = len(self.items) - 1
            self.beginRemoveRows(QModelIndex(), row, row)
            self.items.pop()
            self.endRemoveRows()


def _table(model: Rows) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(32)
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
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(8)
    return widget, layout


def _journal_row(entry: JournalEntry) -> tuple[str, ...]:
    operation = ("独立核对" if entry.verification.independent else "自动回读") if entry.verification else entry.operation
    outcome = entry.write.status if entry.write is not None and entry.write.status == WriteStatus.REJECTED else entry.outcome
    return (entry.time.strftime("%m-%d %H:%M:%S"), operation, entry.target,
            entry.function or "—", f"{entry.elapsed_ms} ms", outcome)


class MainWindow(QMainWindow):
    workspace_changed = Signal()
    stopped = Signal()

    def __init__(self, bridge: IoBridge, protocols: tuple[DesktopProtocol, ...],
                 history: LocalHistory | None = None, return_home: Callable[[], None] | None = None,
                 *, embedded=False):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · 客户端")
        self.resize(1120, 800)
        self.setMinimumWidth(0 if embedded else 640)
        self._bridge = bridge
        self._managed = isinstance(bridge.session, SessionManager)
        self._active_device_id = None
        self._device_states = {}
        self._device_tokens = {}
        self._retired_tokens = set()
        self._sample_stores = {}
        self._sample_definitions = {}
        self._last_reads = {}
        self._quick_values = {}
        self._all_entries = []
        self._log_retention = LogRetention()
        self._write_context = None
        self._latest_write = None
        self._diagnostics = None
        self._pending_removals = {}
        self._device_errors = {}
        self._connection_configs = {}
        self._history = history
        self._project_mode = bool(getattr(history, "project_mode", False))
        self._return_home = return_home
        self._embedded = embedded
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
        self.detail.setPlaceholderText("选择一条日志查看详情和 TX/RX 字节")

        self.results = Rows[Point](
            ("地址", "点位", "值", "十六进制", "二进制"), self._result_row,
            (2, 3), (3, 4),
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

    @property
    def _session(self):
        return self._bridge.session.for_device(self._active_device_id) if self._managed else self._bridge.session

    def _device_for_profile(self, profile):
        return next((item for item in self._history.workspace.project.devices if item.profile.key == profile.key), None)

    def _validate_runtime_config(self, profile):
        if not self._managed:
            return
        device = self._device_for_profile(profile)
        if device is None:
            return
        state = self._device_states.get(device.id, SessionState())
        config = self._connection_configs.get(device.id)
        if state.connection != ConnectionState.OFFLINE and config is not None and validate_profile(profile) != config:
            raise ValueError(f"设备「{device.name}」正在运行，请先断开再修改连接参数")

    def _samples_for(self, device_id):
        store = self._sample_stores.setdefault(device_id, SampleStore())
        definitions = self._history.workspace.project.point_tables
        if self._sample_definitions.get(device_id) is not definitions:
            store.sync(tuple(table for table in definitions if table.device_id == device_id))
            self._sample_definitions[device_id] = definitions
        return store

    def _bind_device(self, device_id):
        if not self._managed or device_id == self._active_device_id:
            return
        previous = self._active_device_id
        if previous is not None:
            self._device_states[previous] = self._state
            self._last_reads[previous] = self._last_read
            self._quick_values[previous] = tuple(self.results.items)
        self._active_device_id = device_id
        self._state = self._device_states.get(device_id, SessionState(device_id=device_id or ""))
        self._last_read = None
        self.results.reset()
        pane = self.point_table_pane
        if pane is not None:
            pane.samples = self._samples_for(device_id)
            pane.sample_device_id = device_id or "__draft__"
            pane._definitions = None
            pane._pending.clear()
            pane._dirty.clear()
        self._show_device_journal()

    def _restore_device_read(self):
        update = self._last_reads.get(self._active_device_id)
        if update is not None and update.command.target == self._form.target():
            self._last_read = update
            self.results.reset(self._quick_values.get(self._active_device_id, ()))

    def workspace_device_status(self, device_id):
        return self._device_states.get(device_id, SessionState()).connection, self._device_errors.get(device_id, "")

    def select_workspace_device(self, device_id, *, points=False, table_id=None):
        if device_id != self._active_device_id:
            device = next((item for item in self._history.workspace.project.devices if item.id == device_id), None)
            if device is None or device_id in self._pending_removals:
                return False
            if self._history.profiles() != self._profiles:
                self._sync_profiles()
            index = next((i for i in range(self.recent_connections.count())
                          if self.recent_connections.itemData(i).key == device.profile.key), -1)
            if index < 0 or not self._select_profile(index):
                return False
        if table_id is not None:
            index = self.point_table_pane.tables.findData(table_id)
            if index < 0:
                return False
            self.point_table_pane.tables.setCurrentIndex(index)
        self.point_views.setCurrentIndex(int(points))
        return True

    def _build(self):
        root = QWidget()
        root.setObjectName("workspace")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        masthead = QFrame(root)
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(16, 12, 16, 12)
        header.setSpacing(8)
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
        if self._embedded:
            masthead.hide()
        else:
            layout.addWidget(masthead)

        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(8)
        layout.addLayout(body, 1)

        connection, connection_layout = _panel()
        connection_header = QHBoxLayout()
        connection_header.addWidget(_heading("设备连接"), 0 if self._embedded else 1)
        if self._embedded:
            connection_header.addWidget(self.connection_status)
            connection_header.addWidget(self.delete_recent_button)
            connection_header.addStretch()
            self.recent_connections.hide()
        elif self._history is not None:
            recent_label = QLabel("项目设备" if self._project_mode else "最近连接")
            recent_label.setObjectName("fieldLabel")
            recent_label.setBuddy(self.recent_connections)
            connection_header.addWidget(recent_label)
            connection_header.addWidget(self.recent_connections)
            connection_header.addWidget(self.delete_recent_button)
        connection_layout.addLayout(connection_header)
        settings = QHBoxLayout()
        settings.setSpacing(8)
        add_field(settings, "协议", self.protocol)
        settings.addWidget(self.connection_forms, 1)
        if not self._embedded:
            settings.addWidget(self.connection_button, alignment=Qt.AlignmentFlag.AlignBottom)
        connection_layout.addLayout(settings)
        if self._managed:
            controls = QHBoxLayout()
            if self._embedded:
                controls.addWidget(self.connection_button)
            self.new_device_button = QPushButton("新增设备")
            self.connect_all_button = QPushButton("连接全部")
            self.disconnect_all_button = QPushButton("断开全部")
            for button, callback in ((self.new_device_button, self._new_device),
                                     (self.connect_all_button, self._connect_all),
                                     (self.disconnect_all_button, self._disconnect_all)):
                button.clicked.connect(callback)
                controls.addWidget(button)
            self.batch_status = QLabel()
            self.batch_status.setTextFormat(Qt.TextFormat.PlainText)
            controls.addWidget(self.batch_status, 1)
            self.overview = QLabel()
            if self._embedded:
                self.overview.hide()
            else:
                controls.addWidget(self.overview)
            connection_layout.addLayout(controls)
        body.addWidget(connection)

        self.write_summary, summary_layout = _panel()
        summary = QHBoxLayout()
        evidence = QVBoxLayout()
        self.write_state = QLabel()
        self.verification_state = QLabel()
        for label in (self.write_state, self.verification_state):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            evidence.addWidget(label)
        summary.addLayout(evidence, 1)
        self.verify_latest = QPushButton("独立读取核对")
        self.verify_latest.setToolTip("读取上一次写入的目标并核对原始值")
        self.verify_latest.clicked.connect(self._verify_latest_write)
        summary.addWidget(self.verify_latest)
        summary_layout.addLayout(summary)
        body.addWidget(self.write_summary)

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
        self.bit_button = QPushButton("位查看…")
        self.bit_button.setToolTip("选择读取结果，查看每个 bit 的 0/1 状态")
        self.bit_button.clicked.connect(lambda: show_bit_inspector(self, self._bit_reading))
        result_title.addWidget(self.bit_button)
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
                self._poll_table, self._stop_all_polling, embedded=self._embedded,
            )
            self.point_views = QTabWidget()
            self.point_views.setAccessibleName("点位视图")
            self.point_views.addTab(quick, "快速读写")
            self.point_views.addTab(self.point_table_pane, "点表")
            content = self.point_views
        self.journal_panel, journal_layout = _panel()
        if not self._embedded:
            journal_layout.addWidget(_heading("请求日志"))
        diagnostics = QPushButton("请求诊断…")
        diagnostics.clicked.connect(self._open_diagnostics)
        self.verify_selected = QPushButton("核对所选写入")
        self.verify_selected.setToolTip("读取所选写入记录的原目标")
        self.verify_selected.clicked.connect(self._verify_selected_write)
        self.journal_view = JournalView(self.log_table, self.detail, "客户端 · 请求日志与报文")
        self.journal_view.actions.insertWidget(0, diagnostics)
        self.journal_view.actions.insertWidget(1, self.verify_selected)
        journal_layout.addWidget(self.journal_view, 1)
        if self._embedded:
            body.addWidget(content, 1)
        else:
            split = QSplitter(Qt.Orientation.Vertical)
            split.addWidget(content)
            split.addWidget(self.journal_panel)
            split.setChildrenCollapsible(False)
            split.setHandleWidth(8)
            split.setSizes([350, 260])
            body.addWidget(split, 1)
        self.setCentralWidget(root)

    def _result_row(self, point):
        kind = target_kind(self._last_read.command.target) if self._last_read else ""
        return point.address, point.label, str(point.value), point.hex_value, binary_text(point.value, kind)

    def _bit_reading(self):
        index = self.result_table.currentIndex()
        if not index.isValid() or index.row() >= len(self.results.items):
            return None
        point = self.results.items[index.row()]
        update = self._last_read
        kind = target_kind(update.command.target) if update else ""
        context = f"解码值：{point.value}；连接：{self._state.connection}"
        if update:
            context += f"；采样：{update.entry.time:%H:%M:%S}"
            if update.points is None:
                context += "；保留的上次成功结果"
        return BitReading(point.label, bit_pattern(point.value, kind), context)

    def _history_failure(self, message: str):
        logging.warning(message, exc_info=True)
        self.statusBar().showMessage(message, 8000)

    def _load_history(self):
        if self._history is None:
            return
        try:
            if self._project_mode:
                log = self._history.read_log()
                entries, self._log_retention = log.entries, log.retention
            else:
                entries = self._history.recent_entries()
            self.journal.reset(entries)
            self._all_entries = list(entries)
        except OSError:
            self._log_retention = replace(self._log_retention, incomplete=True)
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
            return False
        if self._project_mode and self.before_close is not None:
            if not self.before_close():
                self._sync_profiles()
                return False
            key = profile.key
            profiles = self._history.profiles()
            self._show_profiles(profiles)
            index = next(i for i in range(self.recent_connections.count())
                         if self.recent_connections.itemData(i).key == key)
            profile = self.recent_connections.itemData(index)
        if self._managed:
            device = self._device_for_profile(profile)
            self._bind_device(device.id if device else None)
        self.recent_connections.setCurrentIndex(index)
        protocol_index = self.protocol.findData(profile.protocol)
        form = self._forms[protocol_index]
        try:
            with QSignalBlocker(form.query):
                form.restore_profile(profile.fields)
        except (ClientError, KeyError, ValueError):
            self._history_failure("无法恢复此连接记录，请检查配置")
            self._refresh()
            return False
        self.protocol.setCurrentIndex(protocol_index)
        self.interval.setValue(profile.interval_ms)
        self._query_changed()
        if self._managed:
            self._restore_device_read()
            self._show_device_journal()
            self._refresh()
        return True

    def _sync_profiles(self):
        self._show_profiles(self._history.profiles())
        selected = next((device.profile.key for device in self._history.workspace.project.devices
                         if device.id == self._active_device_id), None)
        self.recent_connections.setCurrentIndex(next((i for i in range(self.recent_connections.count())
            if self.recent_connections.itemData(i).key == selected), -1))
        self._refresh()

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
        if self._managed:
            question += "\n若设备仍在运行，将先断开并清理该设备任务。"
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
        if self._managed:
            device = self._device_for_profile(profile)
            if device is not None:
                self._pending_removals[device.id] = profile
                if device.id == self._active_device_id:
                    self._state = replace(self._state, connection=ConnectionState.CLOSING)
                self._bridge.submit(self._bridge.session.remove(device.id))
                self._refresh()
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
            return True
        try:
            profile = ConnectionProfile(self._registration.key, endpoint, form.profile_fields(), self.interval.value())
            self._validate_runtime_config(profile)
            profiles = self._history.remember(profile)
        except OSError:
            self._history_failure("无法保存最近连接")
            return True  # Local history is best-effort and must not block I/O.
        except (ClientError, ValueError) as error:
            self.feedback.setText(f"无法保存设备配置 · {error}")
            return False
        if profiles != self._profiles:
            self._show_profiles(profiles)
        key = self._registration.key, endpoint
        self.recent_connections.setCurrentIndex(next(
            (index for index in range(self.recent_connections.count())
             if self.recent_connections.itemData(index).key == key), -1,
        ))
        if self._managed:
            device = self._device_for_profile(profile)
            self._bind_device(device.id)
        return True

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
            profile = ConnectionProfile(item.protocol.key, endpoint, form.profile_fields(), self._form_intervals[index])
            self._validate_runtime_config(profile)
            profiles.append(profile)
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
        if self._state.connection == ConnectionState.ONLINE or (self._managed and self._state.connection == ConnectionState.CONNECTING):
            self._state = replace(self._state, connection=ConnectionState.CLOSING)
            self._bridge.submit(self._session.disconnect())
        elif self._state.connection == ConnectionState.OFFLINE:
            try:
                config, endpoint = self._form.connection_config()
            except ClientError as error:
                self.feedback.setText(str(error))
                self._refresh()
                return
            self._query_changed()
            if not self._remember_connection(endpoint):
                self._refresh()
                return
            if self._managed and self._state.connection != ConnectionState.OFFLINE:
                self._restore_device_read()
                self._refresh()
                return
            self._state = replace(self._state, connection=ConnectionState.CONNECTING, endpoint=endpoint)
            self._bridge.submit(self._session.connect(self._registration, config, endpoint))
        self._refresh()

    def _read(self):
        self._execute(Command(self._form.target()))

    def _write(self):
        target = self._form.target()
        state = self._state
        command = self._form.confirm_write(self, state.endpoint, target)
        if command is not None and self._same_connection(state):
            self._execute(command)

    def _table_device(self):
        try:
            _, endpoint = self._form.connection_config()
        except ClientError:
            return None
        return next((device for device in self._history.workspace.project.devices
                     if device.profile.key == (self._registration.key, endpoint)
                     and (not self._managed or device.id == self._active_device_id)), None)

    def _save_table_device(self):
        try:
            _, endpoint = self._form.connection_config()
            profile = ConnectionProfile(self._registration.key, endpoint, self._form.profile_fields(), self.interval.value())
            self._validate_runtime_config(profile)
            profiles = self._history.remember(profile)
            if self._managed:
                self._bind_device(self._device_for_profile(profile).id)
                self._sync_profiles()
            else:
                self._show_profiles(profiles)
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
        self._bridge.submit(self._session.execute_table(table.id, commands))

    def _write_point(self, point):
        state = self._state
        if state.connection != ConnectionState.ONLINE or not self._registration.writable:
            return
        command = self._form.confirm_write(self, state.endpoint, point.target, point)
        if command is not None and self._same_connection(state):
            self._execute(command)

    def _selected_entry(self):
        index = self.log_table.currentIndex()
        return self.journal.items[index.row()] if index.isValid() else None

    def _can_verify_write(self, entry):
        return bool(entry is not None and entry.write is not None and entry.write.target is not None
                    and entry.write.status in (WriteStatus.ACKNOWLEDGED, WriteStatus.UNKNOWN)
                    and entry.protocol == self._registration.key and entry.endpoint == self._state.endpoint
                    and (not self._managed or entry.device_id == self._active_device_id)
                    and self._state.connection == ConnectionState.ONLINE and not self._closing
                    and self._state.queued < self._state.queue_capacity)

    def _verify_write(self, entry):
        if self._can_verify_write(entry):
            self._state = replace(self._state, busy=True, queued=self._state.queued + 1)
            self._refresh()
            self._bridge.submit(self._session.verify_write(entry))

    def _verify_latest_write(self):
        self._verify_write(self._latest_write)

    def _verify_selected_write(self):
        self._verify_write(self._selected_entry())

    def _execute(self, command: Command, interval_ms: int | None = None):
        self.feedback.clear()
        self._remember_connection(self._state.endpoint)
        self._state = replace(self._state, busy=True, queued=self._state.queued + int(interval_ms is None),
                              polling=self._state.polling or interval_ms is not None)
        self._refresh()
        self._bridge.submit(self._session.execute(command, interval_ms))

    def _same_connection(self, state):
        return (self._state.connection == ConnectionState.ONLINE and self._state.generation == state.generation
                and self._state.endpoint == state.endpoint and self._state.device_id == state.device_id
                and self._state.session_id == state.session_id and not self._closing)

    def _poll_table(self, table, start):
        if start:
            self._state = replace(self._state, polling_groups=self._state.polling_groups + (table.id,))
            commands = tuple((point.id, Command(point.target)) for point in table.points)
            self._bridge.submit(self._session.start_table_polling(table.id, commands, table.interval_ms))
        else:
            self._bridge.submit(self._session.stop_table_polling(table.id))
        self._refresh()

    def _stop_all_polling(self):
        self._bridge.submit(self._session.stop_all_polling())

    def _toggle_poll(self):
        if self._state.polling:
            self._stopping_poll = True
            self._refresh()
            self._bridge.submit(self._session.stop_polling())
        else:
            self._execute(Command(self._form.target()), self.interval.value())

    def _new_device(self):
        if self.before_close is not None and not self.before_close():
            return
        self._bind_device(None)
        self._form.connection.host.clear()
        self.recent_connections.setCurrentIndex(-1)
        self._query_changed()

    def _connect_all(self):
        if self.before_close is not None and not self.before_close():
            return
        specifications = []
        try:
            for device in self._history.workspace.project.devices:
                protocol = next(item.protocol for item in self._protocols if item.protocol.key == device.profile.protocol)
                config = validate_profile(device.profile)
                specifications.append((device.id, protocol, config, device.profile.endpoint))
        except (ClientError, ValueError, StopIteration) as error:
            self.feedback.setText(f"无法准备设备配置 · {error}")
            self._refresh()
            return
        self.batch_status.setText("正在逐台连接…")
        self._bridge.submit(self._bridge.session.connect_all(tuple(specifications)))

    def _disconnect_all(self):
        self.batch_status.setText("正在清理全部连接…")
        self._bridge.submit(self._bridge.session.disconnect_all())

    def _show_device_journal(self):
        if self._managed:
            self.journal.reset(tuple(entry for entry in self._all_entries if self._entry_visible(entry)))
            self.detail.clear()

    def _entry_visible(self, entry):
        if not self._managed or self._active_device_id is None:
            return True
        if entry.device_id:
            return entry.device_id == self._active_device_id
        device = next((item for item in self._history.workspace.project.devices if item.id == self._active_device_id), None)
        return bool(device and entry.protocol == device.profile.protocol and entry.endpoint == device.profile.endpoint)

    def report_log(self) -> RetainedLog[JournalEntry]:
        return RetainedLog(tuple(self._all_entries), self._log_retention)

    def _append_journal(self, entry):
        cleared = False
        if self._history is not None:
            try:
                if self._history.append_entry(entry):
                    self._all_entries.clear()
                    self.journal.reset()
                    self.detail.clear()
                    cleared = True
                    self._log_retention = replace(self._log_retention, rotated_at=datetime.now().astimezone(), clipped=False)
            except OSError:
                self._log_retention = replace(self._log_retention, incomplete=True)
                self._history_failure("无法保存本地请求日志")
        self._all_entries.insert(0, entry)
        expired = self._all_entries[VISIBLE_LOG_ROWS:]
        if expired and not self._log_retention.clipped:
            self._log_retention = replace(self._log_retention, clipped=True)
        del self._all_entries[VISIBLE_LOG_ROWS:]
        if self._entry_visible(entry):
            self.journal.prepend(entry)
        if self._managed:
            for old in expired:
                self.journal.discard_oldest(old)
        if (entry.write is not None or entry.verification is not None or cleared
                or any(item is self._latest_write for item in expired)):
            self._refresh_write_summary(force=True)
        if entry.verification is not None and self.log_table.currentIndex().isValid():
            self._show_detail(self.log_table.currentIndex(), None)

    def _complete_removal(self, device_id):
        profile = self._pending_removals.pop(device_id, None)
        if profile is not None:
            try:
                self._history.forget(profile)
            except OSError:
                self._history_failure("设备已断开，但无法移除配置")
                return
        token = self._device_tokens.pop(device_id, None)
        if token:
            self._retired_tokens.add(token)
        if device_id == self._active_device_id:
            self._bind_device(None)
        for form in self._forms:
            try:
                _, endpoint = form.connection_config()
            except ClientError:
                continue
            if profile is not None and endpoint == profile.endpoint:
                form.connection.host.clear()
        for cache in (self._device_states, self._last_reads, self._quick_values, self._sample_stores,
                      self._sample_definitions, self._device_errors, self._connection_configs):
            cache.pop(device_id, None)
        self._show_profiles(self._history.profiles())
        self._show_device_journal()
        self._refresh()

    def _handle_device_event(self, event):
        payload, device_id = event.payload, event.device_id
        if isinstance(payload, BatchResult):
            succeeded = sum(result.successful for result in payload.results)
            failed = len(payload.results) - succeeded
            self.batch_status.setText(f"批量{payload.operation}：成功 {succeeded}，失败 {failed}")
            names = {item.id: item.name for item in self._history.workspace.project.devices}
            self.batch_status.setToolTip("\n".join(
                f"{names.get(result.device_id, result.device_id)}：{'成功' if result.successful else '失败'} {result.detail}"
                for result in payload.results))
            return
        if not any(device.id == device_id for device in self._history.workspace.project.devices):
            return
        if event.runtime_id in self._retired_tokens:
            return
        expected = self._device_tokens.get(device_id)
        if event.runtime_id and expected is not None and event.runtime_id != expected:
            return
        if event.runtime_id:
            self._device_tokens[device_id] = event.runtime_id
        if isinstance(payload, DeviceRemoved):
            self._complete_removal(device_id)
            return
        state = self._device_states.get(device_id, SessionState(device_id=device_id))
        if isinstance(payload, JournalEntry):
            self._append_journal(payload)
            if event.generation >= state.generation:
                if payload.error:
                    self._device_errors[device_id] = payload.detail or payload.outcome
                elif payload.operation in ("连接", "读取", "写入", "断开"):
                    self._device_errors.pop(device_id, None)
            if device_id == self._active_device_id and (payload.error or payload.operation == "写入"):
                self.feedback.setText(f"{payload.outcome} · {payload.detail}" if payload.detail else payload.outcome)
        elif event.generation < state.generation:
            return
        elif isinstance(payload, SessionState):
            self._device_states[device_id] = payload
            if event.config is not None:
                self._connection_configs[device_id] = event.config
            if device_id == self._active_device_id:
                self._handle_event(payload)
                return
            store = self._samples_for(device_id)
            store.set_generation(payload.generation)
            if payload.connection != ConnectionState.ONLINE:
                store.disconnect()
        elif isinstance(payload, ReadUpdate):
            previous = self._last_reads.get(device_id)
            if payload.points is not None:
                self._last_reads[device_id] = payload
                self._quick_values[device_id] = payload.points
            elif previous is not None and previous.command.target == payload.command.target:
                self._last_reads[device_id] = replace(previous, points=None)
            if device_id == self._active_device_id:
                self._handle_event(payload)
                return
        elif isinstance(payload, TableReadUpdate):
            if device_id == self._active_device_id:
                self._handle_event(payload)
                return
            store = self._samples_for(device_id)
            store.set_generation(state.generation)
            store.update(payload)
        elif isinstance(payload, OperationRejected) and device_id == self._active_device_id:
            self._handle_event(payload)
            return
        self._refresh()

    def _refresh_device_labels(self):
        devices = {item.profile.key: item for item in self._history.workspace.project.devices}
        for index in range(self.recent_connections.count()):
            profile = self.recent_connections.itemData(index)
            device = devices.get(profile.key)
            if device is None:
                continue
            state = self._device_states.get(device.id, SessionState())
            self.recent_connections.setItemText(index, f"[{state.connection}] {self.protocol.itemText(self.protocol.findData(profile.protocol))} · {profile.endpoint}")
            self.recent_connections.setItemData(index, self._device_errors.get(device.id) or profile.endpoint, Qt.ItemDataRole.ToolTipRole)
        states = [self._device_states.get(item.id, SessionState()) for item in devices.values()]
        online = sum(state.connection == ConnectionState.ONLINE for state in states)
        pending = sum(state.connection in (ConnectionState.CONNECTING, ConnectionState.CLOSING) for state in states)
        self.overview.setText(f"已连接 {online}/{len(devices)} · 处理中 {pending}")
        self.new_device_button.setEnabled(not self._closing)
        self.connect_all_button.setEnabled(bool(devices) and not self._closing)
        self.disconnect_all_button.setEnabled(bool(online or pending) and not self._closing)

    def _handle_event(self, event):
        if isinstance(event, DeviceEvent):
            self._handle_device_event(event)
            return
        if isinstance(event, SessionState):
            if event.generation < self._state.generation:
                return
            self._state = event
            if not event.polling:
                self._stopping_poll = False
        elif isinstance(event, JournalEntry):
            self._append_journal(event)
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
        self.verify_selected.setEnabled(self._can_verify_write(self._selected_entry()))
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
        if entry.session_id:
            lines.append("  ".join(filter(None, (f"设备 {entry.device_id}" if entry.device_id else "",
                                                   f"会话 {entry.session_id}  代次 {entry.generation}"))))
        if entry.detail:
            lines.append(entry.detail)
        lines.extend(write_evidence_lines(entry))
        if entry.write is not None and (checked := self._verification_for(entry)) is not None:
            lines.append(f"{verification_summary(checked)} · 核对请求 #{checked.request_id}")
        for sending, data in entry.packets:
            lines.append(f"{'TX' if sending else 'RX'}  {data.hex(' ').upper()}")
        self.detail.setPlainText("\n".join(lines))

    def _verification_for(self, entry):
        return next((item for item in self._all_entries
                     if item.verification is not None and item.device_id == entry.device_id
                     and item.verification.write_session_id == entry.session_id
                     and item.verification.write_request_id == entry.request_id), None)

    def _refresh_write_summary(self, *, force=False):
        context = (self._active_device_id, self._registration.key, self._state.endpoint)
        if force or context != self._write_context:
            self._write_context = context
            device, protocol, endpoint = context
            self._latest_write = entry = next((item for item in self.journal.items
                if item.write is not None and item.protocol == protocol and (not endpoint or item.endpoint == endpoint)
                and (not self._managed or item.device_id == device)), None)
            self.write_summary.setVisible(entry is not None)
            if entry is not None:
                write = entry.write
                self.write_state.setText(f"写入 #{entry.request_id} · {entry.target} · {write.status}")
                self.write_state.setToolTip(entry.endpoint)
                checked = self._verification_for(entry)
                if checked is not None:
                    message = verification_summary(checked)
                elif write.status == WriteStatus.UNKNOWN:
                    message = "无法验证 · 可独立读取核对"
                elif write.status != WriteStatus.ACKNOWLEDGED:
                    message = "无法验证 · 设备未确认写入"
                elif write.options is not None and write.options.delay_ms is not None:
                    message = ("等待自动回读" if entry.session_id == self._state.session_id
                               and self._state.connection == ConnectionState.ONLINE else "无法验证 · 缺少回读记录")
                else:
                    message = "未请求自动回读"
                self.verification_state.setText(message)
        self.verify_latest.setEnabled(self._can_verify_write(self._latest_write))
        self.verify_selected.setEnabled(self._can_verify_write(self._selected_entry()))

    def _open_diagnostics(self):
        from .diagnostics_dialog import DiagnosticsDialog

        if self._diagnostics is None:
            names = {device.id: f"{device.name or device.profile.endpoint} · {device.profile.endpoint}"
                     for device in self._history.workspace.project.devices} if self._managed else {}
            index = self.log_table.currentIndex()
            entry = self.journal.items[index.row()] if index.isValid() else None
            self._diagnostics = DiagnosticsDialog(
                self, lambda: self._all_entries,
                lambda row: DiagnosticRow.from_client(row, names.get(row.device_id, "")),
                selected=entry, device=(self._active_device_id,) if self._active_device_id else None,
            )
            self._diagnostics.finished.connect(self._diagnostics_closed)
        self._diagnostics.show()
        self._diagnostics.raise_()
        self._diagnostics.activateWindow()

    def _diagnostics_closed(self):
        self._diagnostics = None

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
        cancelling = self._managed and state.connection == ConnectionState.CONNECTING
        self.connection_button.setText("取消连接" if cancelling else "断开连接" if online else "连接")
        self.connection_button.setEnabled((online or offline or cancelling) and not self._closing)
        self.protocol.setEnabled(offline and len(self._forms) > 1 and not self._closing)
        history_available = (self._managed or offline) and not self._closing
        self.recent_connections.setEnabled(history_available and self.recent_connections.count() > 0)
        self.delete_recent_button.setEnabled(history_available and self.recent_connections.currentIndex() >= 0
                                            and self._active_device_id not in self._pending_removals)
        self.connection_forms.setEnabled(offline and not self._closing)
        self.query_forms.setEnabled((offline or idle) and not self._closing)
        self.read_button.setEnabled(idle)
        self.write_button.setEnabled(idle and self._registration.writable and self._form.can_write())
        self._refresh_write_summary()
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
        if self._managed:
            if self._active_device_id is not None:
                self._device_states[self._active_device_id] = state
            self._refresh_device_labels()
            self.workspace_changed.emit()

    def _io_stopped(self):
        self._stopped = True
        if not self._embedded:
            self.close()
        self.stopped.emit()

    def shutdown_embedded(self):
        self._closing = True
        self._refresh()
        self._bridge.shutdown()

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
