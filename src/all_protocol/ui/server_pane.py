from collections import deque
from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QWidget,
)

from ..application.diagnostics import DiagnosticRow
from ..application.faults import FaultAction, FaultUpdate
from ..application.journal import LogRetention, RetainedLog
from ..application.server_contracts import ListenerState, ListenerUpdate, RequestMetadata, RuntimeChange, ServerLifecycle
from ..application.simulation import SimulationUpdate
from ..storage import VISIBLE_LOG_ROWS
from .main_window import Rows, _heading, _panel, _table
from .diagnostics_dialog import DiagnosticsDialog
from .fault_dialog import FaultDialog
from .simulation_dialog import SimulationDialog
from .widgets import add_field, spinbox


class ServerPane(QWidget):
    listener_changed = Signal()
    simulation_changed = Signal()

    def __init__(self, bridge, storage, default_plan):
        super().__init__()
        self.bridge = bridge
        self._storage = storage
        self._workspace_log = deque(maxlen=VISIBLE_LOG_ROWS) if getattr(storage, "project_mode", False) else None
        self._log_retention = LogRetention()
        self._saved_plan = default_plan
        self.state = ListenerState.STOPPED
        self.endpoint = ""
        self.listener_error = ""
        self.simulation_update: SimulationUpdate | None = None
        self.fault_update = FaultUpdate()
        self._diagnostics = None
        self._load_error = False

    @property
    def configured_endpoint(self):
        host = self.host.text()
        return f"[{host}]:{self.port.value()}" if ":" in host else f"{host}:{self.port.value()}"

    def configuration_plan(self):
        return self._plan()

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
        scene = QHBoxLayout()
        self.scene_button = self._button(scene, "场景模拟…", self._open_simulation)
        self.scene_status = QLabel()
        self.scene_status.setObjectName("resultContext")
        scene.addWidget(self.scene_status, 1)
        layout.addLayout(scene)
        faults = QHBoxLayout()
        self.fault_button = self._button(faults, "故障注入…", self._open_faults)
        self.reset_fault = self._button(faults, "重置故障", self._reset_fault)
        self.fault_status = QLabel()
        self.fault_status.setWordWrap(True)
        self.fault_status.setObjectName("resultContext")
        faults.addWidget(self.fault_status, 1)
        layout.addLayout(faults)
        self.feedback = QLabel()
        self.feedback.setObjectName("warning")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        layout.addWidget(self.feedback)
        self.host.textChanged.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        return panel

    def _open_simulation(self):
        dialog = SimulationDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _open_faults(self):
        dialog = FaultDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _reset_fault(self):
        self.bridge.submit(self.bridge.session.configure_fault(None))

    def _refresh_fault(self):
        update = self.fault_update
        running = self.state == ListenerState.LISTENING
        self.fault_button.setEnabled(running)
        self.reset_fault.setEnabled(running and update.rule is not None)
        if update.rule is None:
            self.fault_status.setText("故障未启用")
            return
        rule = update.rule
        scope = f"剩余 {update.remaining} 次" if update.remaining is not None else f"限时 {rule.duration_ms} ms"
        status = scope if update.active else "已结束"
        action = f"{rule.action} {rule.delay_ms} ms" if rule.action == FaultAction.DELAY else str(rule.action)
        self.fault_status.setText(f"{action} · {rule.operation} · {status} · 已触发 {update.triggered} 次")

    def _refresh_simulation(self):
        update = self.simulation_update
        if update is not None:
            status = str(update.state)
            if update.held:
                status += f" · {len(update.held)} 点已接管"
            if update.faults:
                status += f" · {len(update.faults)} 点异常"
        else:
            configured = next(self._plan().simulation_points(configured_only=True), None)
            status = "启动服务后可运行场景" if configured else "场景未配置"
        self.scene_status.setText(status)

    def _journal_panel(self, headers, format_row, numbers=(), monospace=()):
        panel, layout = _panel()
        self.journal_heading = _heading("请求日志")
        header = QHBoxLayout()
        header.addWidget(self.journal_heading)
        self._button(header, "请求诊断…", self._open_diagnostics)
        header.addStretch()
        layout.addLayout(header)
        self.journal_panel = panel
        self.requests = Rows(headers, format_row, numbers, monospace)
        table = self.request_table = _table(self.requests)
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

    def show_workspace_values(self, current):
        self.host.setReadOnly(current)
        self.port.setReadOnly(current)
        self.point_table.setColumnHidden(self.points.columnCount() - 2, current)
        self.point_table.setColumnHidden(self.points.columnCount() - 1, not current)
        for control in self.plan_controls:
            control.setVisible(not current)
        self.edit_value.setVisible(current)

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
            self.requests.reset(self._storage.recent_events())
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
        self.listener_error = ""
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
        payload = event.payload
        if isinstance(payload, ListenerUpdate):
            previous, self.state = self.state, payload.state
            if self.state == ListenerState.STOPPED:
                self.simulation_update = None
                self.fault_update = FaultUpdate()
            self.endpoint = event.endpoint
            self.listener_error = payload.error
            if payload.error:
                self._feedback(payload.error)
            if previous != self.state:
                self._state_changed()
            self._refresh()
        elif isinstance(payload, (RequestMetadata, ServerLifecycle)):
            try:
                if self._storage.append_event(event):
                    self.requests.reset()
                    self.detail.clear()
                    if self._workspace_log is not None:
                        self._workspace_log.clear()
                        self._log_retention = replace(self._log_retention, rotated_at=datetime.now().astimezone(),
                                                      clipped=False)
            except OSError as error:
                self._log_retention = replace(self._log_retention, incomplete=True)
                self._feedback(f"保存请求日志失败 · {error}")
            self.requests.prepend(event)
            if self._workspace_log is not None:
                if len(self._workspace_log) == VISIBLE_LOG_ROWS and not self._log_retention.clipped:
                    self._log_retention = replace(self._log_retention, clipped=True)
                self._workspace_log.append(event)
        elif isinstance(payload, RuntimeChange):
            self._runtime_changed(payload.details)
        elif isinstance(payload, SimulationUpdate):
            self.simulation_update = payload
            self._refresh_simulation()
            self.simulation_changed.emit()
        elif isinstance(payload, FaultUpdate):
            self.fault_update = payload
            self._refresh_fault()

    def report_log(self):
        return RetainedLog(tuple(reversed(self._workspace_log)) if self._workspace_log is not None else (),
                           self._log_retention)

    def _show_request(self, index, _previous):
        if index.isValid():
            payload = self.requests.items[index.row()].payload
            rx = payload.rx.hex(" ").upper() if isinstance(payload, RequestMetadata) else ""
            tx = payload.tx.hex(" ").upper() if isinstance(payload, RequestMetadata) else ""
            self.detail.setPlainText(f"RX  {rx or '—'}\nTX  {tx or '—'}")
        else:
            self.detail.clear()

    def _open_diagnostics(self):
        if self._diagnostics is None:
            index = self.request_table.currentIndex()
            entry = self.requests.items[index.row()] if index.isValid() else None
            self._diagnostics = DiagnosticsDialog(self, lambda: self.requests.items, DiagnosticRow.from_server,
                                                  server=True, selected=entry)
            self._diagnostics.finished.connect(self._diagnostics_closed)
        self._diagnostics.show()
        self._diagnostics.raise_()
        self._diagnostics.activateWindow()

    def _diagnostics_closed(self):
        self._diagnostics = None

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
        self._refresh_simulation()
        self._refresh_fault()
        self.listener_changed.emit()
        return idle, running
