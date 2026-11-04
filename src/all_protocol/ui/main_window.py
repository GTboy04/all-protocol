from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from ..application.contracts import (
    ClientError, Command, ConnectionState, JournalEntry, Point, ProtocolRegistration,
    ReadUpdate, SessionState, Target, Value,
)
from .io_bridge import IoBridge


class ProtocolForm(Protocol):
    connection: QWidget
    query: QWidget

    def connection_config(self) -> tuple[object, str]: ...
    def target(self) -> Target: ...
    def can_write(self) -> bool: ...
    def confirm_write(self, parent: QWidget, endpoint: str, target: Target) -> tuple[Value, ...] | None: ...


@dataclass(frozen=True)
class DesktopProtocol:
    protocol: ProtocolRegistration
    create_form: Callable[[], ProtocolForm]


class Rows[T](QAbstractTableModel):
    def __init__(self, headers: tuple[str, ...], format_row: Callable[[T], tuple[str, ...]],
                 right_aligned: tuple[int, ...] = ()):
        super().__init__()
        self.headers = headers
        self.format_row = format_row
        self.right_aligned = right_aligned
        self.items: list[T] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.format_row(self.items[index.row()])[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self.right_aligned:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def reset(self, items: tuple[T, ...] = ()):
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def prepend(self, item: T):
        if len(self.items) == 500:
            self.beginRemoveRows(QModelIndex(), 499, 499)
            self.items.pop()
            self.endRemoveRows()
        self.beginInsertRows(QModelIndex(), 0, 0)
        self.items.insert(0, item)
        self.endInsertRows()


def _table(model: Rows) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.verticalHeader().hide()
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _panel():
    widget = QFrame()
    widget.setObjectName("panel")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(14, 10, 14, 10)
    return widget, layout


def _journal_row(entry: JournalEntry) -> tuple[str, ...]:
    return (entry.time.strftime("%H:%M:%S"), entry.operation, entry.target,
            entry.function or "—", f"{entry.elapsed_ms} ms", entry.outcome)


class MainWindow(QMainWindow):
    def __init__(self, bridge: IoBridge, protocols: tuple[DesktopProtocol, ...]):
        super().__init__()
        self.setWindowTitle("工业协议调试台")
        self.resize(1120, 760)
        self.setMinimumWidth(640)
        self._bridge = bridge
        self._protocols = protocols
        self._forms = tuple(item.create_form() for item in protocols)
        self._state = SessionState()
        self._closing = False
        self._stopped = False
        self._stopping_poll = False
        self._last_read: ReadUpdate | None = None

        self.protocol = QComboBox()
        self.connection_forms = QStackedWidget()
        self.query_forms = QStackedWidget()
        for item, form in zip(protocols, self._forms, strict=True):
            self.protocol.addItem(item.protocol.label, item.protocol.key)
            self.connection_forms.addWidget(form.connection)
            self.query_forms.addWidget(form.query)
            form.query.changed.connect(self._query_changed)

        self.connection_button = QPushButton("连接")
        self.connection_status = QLabel()
        self.read_button = QPushButton("读取")
        self.read_button.setObjectName("primary")
        self.write_button = QPushButton("写入…")
        self.write_button.setObjectName("write")
        self.poll_button = QPushButton("启动轮询")
        self.interval = QSpinBox()
        self.interval.setRange(200, 30000)
        self.interval.setValue(1000)
        self.interval.setSuffix(" ms")
        self.interval.setToolTip("两次读取之间的等待时间")
        self.result_context = QLabel()
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(112)
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
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.log_table = _table(self.journal)
        self.log_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._build()

        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.connection_button.clicked.connect(self._toggle_connection)
        self.read_button.clicked.connect(self._read)
        self.write_button.clicked.connect(self._write)
        self.poll_button.clicked.connect(self._toggle_poll)
        self.log_table.selectionModel().currentRowChanged.connect(self._show_detail)
        bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        bridge.stopped.connect(self._io_stopped, Qt.ConnectionType.QueuedConnection)
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
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("工业协议调试台")
        title.setObjectName("title")
        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(self.connection_status)
        layout.addLayout(header)

        connection, connection_layout = _panel()
        settings = QHBoxLayout()
        protocol_label = QLabel("协议")
        protocol_label.setBuddy(self.protocol)
        settings.addWidget(protocol_label)
        settings.addWidget(self.protocol)
        settings.addWidget(self.connection_forms, 1)
        settings.addWidget(self.connection_button, alignment=Qt.AlignmentFlag.AlignBottom)
        connection_layout.addLayout(settings)
        layout.addWidget(connection)

        query, query_layout = _panel()
        query_layout.addWidget(self.query_forms)
        actions = QHBoxLayout()
        actions.addWidget(self.feedback, 1)
        actions.addWidget(self.read_button)
        actions.addWidget(self.write_button)
        interval_label = QLabel("间隔")
        interval_label.setBuddy(self.interval)
        actions.addWidget(interval_label)
        actions.addWidget(self.interval)
        actions.addWidget(self.poll_button)
        query_layout.addLayout(actions)
        layout.addWidget(query)

        results, result_layout = _panel()
        result_title = QHBoxLayout()
        result_title.addWidget(QLabel("读取结果"), 1)
        result_title.addWidget(self.result_context)
        result_layout.addLayout(result_title)
        result_layout.addWidget(self.result_table)
        journal, journal_layout = _panel()
        journal_layout.addWidget(QLabel("请求日志"))
        journal_layout.addWidget(self.log_table)
        journal_layout.addWidget(self.detail)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(results)
        split.addWidget(journal)
        split.setChildrenCollapsible(False)
        split.setSizes([320, 245])
        layout.addWidget(split, 1)
        self.setCentralWidget(root)

    def _protocol_changed(self, index):
        self.connection_forms.setCurrentIndex(index)
        self.query_forms.setCurrentIndex(index)
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
                return
            self._query_changed()
            self._state = SessionState(ConnectionState.CONNECTING, endpoint)
            self._bridge.submit(self._bridge.session.connect(self._registration, config, endpoint))
        self._refresh()

    def _read(self):
        self._execute(Command(self._form.target()))

    def _write(self):
        target = self._form.target()
        state = self._state
        values = self._form.confirm_write(self, state.endpoint, target)
        if values is not None and self._state == state and not self._closing:
            self._execute(Command(target, values))

    def _execute(self, command: Command, interval_ms: int | None = None):
        self.feedback.clear()
        self._state = replace(self._state, busy=True, polling=interval_ms is not None)
        self._refresh()
        self._bridge.submit(self._bridge.session.execute(command, interval_ms))

    def _toggle_poll(self):
        if self._state.polling:
            self._stopping_poll = True
            self._refresh()
            self._bridge.submit(self._bridge.session.stop_polling())
        else:
            self._execute(Command(self._form.target()), self.interval.value())

    def _handle_event(self, event):
        if isinstance(event, SessionState):
            self._state = event
            if not event.polling:
                self._stopping_poll = False
        elif isinstance(event, JournalEntry):
            self.journal.prepend(event)
            if event.error or event.operation == "写入":
                self.feedback.setText(f"{event.outcome} · {event.detail}" if event.detail else event.outcome)
        elif isinstance(event, ReadUpdate) and event.command.target == self._form.target():
            if event.points is not None:
                self._last_read = event
                self.results.reset(event.points)
            elif self._last_read is not None:
                self._last_read = replace(self._last_read, points=None)
        self._refresh()

    def _show_detail(self, index, _previous):
        if not index.isValid():
            self.detail.clear()
            return
        entry = self.journal.items[index.row()]
        lines = [f"#{entry.request_id}  {entry.operation}  {entry.target}  {entry.outcome}"]
        if entry.detail:
            lines.append(entry.detail)
        for sending, data in entry.packets:
            lines.append(f"{'TX' if sending else 'RX'}  {data.hex(' ').upper()}")
        self.detail.setPlainText("\n".join(lines))

    def _refresh(self):
        state = self._state
        online = state.connection == ConnectionState.ONLINE
        offline = state.connection == ConnectionState.OFFLINE
        idle = online and not state.busy and not self._closing
        self.connection_status.setText(
            f"{state.connection} · {state.endpoint}" if state.endpoint else state.connection
        )
        self.connection_button.setText("断开连接" if online else "连接")
        self.connection_button.setEnabled((online or offline) and not self._closing)
        self.protocol.setEnabled(offline and len(self._forms) > 1 and not self._closing)
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
            if self._last_read.points is None or not online:
                text += " · 旧数据"
        self.result_context.setText(text)

    def _io_stopped(self):
        self._stopped = True
        self.close()

    def closeEvent(self, event: QCloseEvent):
        if self._stopped:
            event.accept()
            return
        event.ignore()
        self._closing = True
        self._refresh()
        self._bridge.shutdown()


STYLE = """
QWidget#workspace { background: #e9eff4; }
QFrame#panel { background: #ffffff; border: 1px solid #d3dee8; border-radius: 5px; }
QLabel { color: #182a3d; }
QLabel#title { color: #18364e; font-size: 21px; font-weight: bold; }
QLabel#warning, QPushButton#write:enabled { color: #9a4e16; }
QPushButton { padding: 5px 12px; }
QPushButton#primary:enabled { background: #12618a; color: white; border: 1px solid #12618a; border-radius: 3px; }
QPushButton#primary:focus { border: 2px solid #182a3d; }
QTableView { alternate-background-color: #f4f8fb; gridline-color: #e3ebf2; }
"""
