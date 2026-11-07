from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..application.server_session import ListenerState, ListenerUpdate
from .main_window import Rows, _heading, _panel, _table
from .widgets import add_field, spinbox


class EditDialog(QDialog):
    def __init__(self, parent, title, commit):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(350)
        self.commit = commit
        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.error = QLabel()
        self.error.setObjectName("warning")
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        try:
            self.commit()
        except ValueError as error:
            self.error.setText(str(error))
            return
        super().accept()


class ServerPane(QWidget):
    listener_changed = Signal()

    def __init__(self, bridge, storage, default_plan, request_type):
        super().__init__()
        self.bridge = bridge
        self._storage = storage
        self._saved_plan = default_plan
        self._request_type = request_type
        self.state = ListenerState.STOPPED
        self._load_error = False

    def _settings(self):
        panel, layout = _panel()
        controls = QHBoxLayout()
        controls.setAlignment(Qt.AlignmentFlag.AlignBottom)
        self.host = QLineEdit()
        self.port = spinbox(1, 65535, self._saved_plan.port)
        for field in (self.host, self.port):
            field.setFont(QFont("Consolas"))
        self.dirty = QLabel()
        self.dirty.setObjectName("resultContext")
        self.status = QLabel()
        self.status.setObjectName("listenerStatus")
        add_field(controls, "监听地址", self.host, 1)
        add_field(controls, "端口", self.port)
        self.listen = self._button(controls, "启动服务", self._toggle_listener)
        self.listen.setObjectName("primary")
        controls.addWidget(self.status)
        controls.addStretch(1)
        controls.addWidget(self.dirty)
        self.save = self._button(controls, "保存方案", self._save_plan)
        if getattr(self._storage, "project_mode", False):
            self.save.setText("应用到项目")
            self.save.setToolTip("暂存方案；请在首页保存项目文件")
        for control in (self.listen, self.status, self.dirty, self.save):
            controls.setAlignment(control, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(controls)
        self.feedback = QLabel()
        self.feedback.setObjectName("warning")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        layout.addWidget(self.feedback)
        self.host.textChanged.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        return panel

    def _journal_panel(self, headers, format_row, numbers=(), monospace=()):
        panel, layout = _panel()
        layout.addWidget(_heading("请求日志"))
        self.requests = Rows(headers, format_row, numbers, monospace)
        table = _table(self.requests)
        table.setAccessibleName("请求日志")
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择请求以查看 RX / TX 报文")
        self.detail.setObjectName("packetDetail")
        self.detail.setAccessibleName("RX / TX 报文详情")
        table.selectionModel().currentRowChanged.connect(self._show_request)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(table)
        split.addWidget(self.detail)
        split.setSizes([710, 350])
        layout.addWidget(split, 1)
        return panel

    @staticmethod
    def _button(layout, title, slot):
        button = QPushButton(title)
        layout.addWidget(button)
        button.clicked.connect(slot)
        return button

    def _load(self):
        try:
            self._saved_plan = self._storage.load_plan()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._load_error = True
            self._feedback(f"无法读取方案 {self._storage.plan_path} · {error}")
        self._load_points(self._saved_plan)
        self.host.setText(self._saved_plan.host)
        self.port.setValue(self._saved_plan.port)
        try:
            self.requests.reset(self._storage.recent_requests())
        except OSError as error:
            self._feedback(f"{self.feedback.text()}\n无法读取请求日志 · {error}".strip())
        self.bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _feedback(self, message):
        self.feedback.setText(message)
        self.feedback.setVisible(bool(message))

    def _save_plan(self):
        plan = self._plan()
        try:
            self._storage.save_plan(plan)
        except (OSError, ValueError) as error:
            self._feedback(f"保存方案失败 · {error}")
            return False
        self._saved_plan = plan
        self._load_error = False
        self._feedback("")
        self._refresh()
        return True

    def confirm_close(self, parent, protocol):
        if self._plan() == self._saved_plan:
            return True
        prompt = QMessageBox(
            QMessageBox.Icon.Question, "未保存的方案", f"离开前如何处理 {protocol} 方案修改？", parent=parent,
        )
        save = prompt.addButton("应用并离开" if getattr(self._storage, "project_mode", False) else "保存并离开",
                                QMessageBox.ButtonRole.AcceptRole)
        discard = prompt.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        prompt.setDefaultButton(cancel)
        prompt.exec()
        choice = prompt.clickedButton()
        return choice == discard or (choice == save and self._save_plan())

    def _toggle_listener(self):
        self._feedback("")
        session = self.bridge.session
        if self.state == ListenerState.STOPPED:
            plan = self._plan()
            self.state = ListenerState.STARTING
            self.bridge.submit(session.start(plan))
        elif self.state == ListenerState.LISTENING:
            self.state = ListenerState.STOPPING
            self.bridge.submit(session.stop())
        self._refresh()

    def _handle_event(self, event):
        if isinstance(event, ListenerUpdate):
            previous, self.state = self.state, event.state
            if event.error:
                self._feedback(event.error)
            if previous != self.state:
                self._state_changed()
            self._refresh()
        elif isinstance(event, self._request_type):
            try:
                if self._storage.append_request(event):
                    self.requests.reset()
                    self.detail.clear()
            except OSError as error:
                self._feedback(f"保存请求日志失败 · {error}")
            self.requests.prepend(event)
        else:
            self._runtime_changed(event)

    def _show_request(self, index, _previous):
        if index.isValid():
            entry = self.requests.items[index.row()]
            self.detail.setPlainText(f"RX  {entry.rx.hex(' ').upper() or '—'}\nTX  {entry.tx.hex(' ').upper() or '—'}")
        else:
            self.detail.clear()

    def _refresh_listener(self):
        idle = self.state == ListenerState.STOPPED
        running = self.state == ListenerState.LISTENING
        self.status.setText(self.state)
        tone = "online" if running else "offline" if idle else "pending"
        if self.status.property("tone") != tone:
            self.status.setProperty("tone", tone)
            style = self.status.style()
            style.unpolish(self.status)
            style.polish(self.status)
        self.listen.setText("停止服务" if running or self.state == ListenerState.STOPPING else "启动服务")
        self.listen.setEnabled(idle or running)
        self.host.setEnabled(idle)
        self.port.setEnabled(idle)
        applied = "已应用到项目" if getattr(self._storage, "project_mode", False) else "已保存"
        self.dirty.setText("有未保存修改" if self._plan() != self._saved_plan else
                           "方案未加载" if self._load_error else applied)
        self.listener_changed.emit()
        return idle, running
