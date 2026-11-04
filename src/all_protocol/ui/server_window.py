from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabBar, QVBoxLayout, QWidget,
)

from ..adapters.modbus_tcp.model import RULES, Area
from ..adapters.modbus_tcp.server import ServerRequest, ValuesChanged
from ..adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from ..application.server_session import ListenerState, ListenerUpdate
from ..storage import ServerStorage
from .io_bridge import IoBridge
from .main_window import Rows, _heading, _panel, _table
from .modbus_tcp_form import spinbox
from .widgets import add_field


def _display(value):
    return "—" if value is None else str(value)


def _point_type(area):
    return "Bool" if RULES[area].value_limit == 1 else "UInt16"


def _value_text(area, value):
    if value is not None and _point_type(area) == "Bool":
        return "开" if value else "关"
    return _display(value)


def _value_field(area, value):
    if _point_type(area) == "Bool":
        field = QComboBox()
        field.addItems(("关", "开"))
        field.setCurrentIndex(value)
        return field, field.currentIndex
    field = spinbox(0, 65535, value)
    return field, field.value


def _request_row(entry: ServerRequest):
    return (entry.time.strftime("%m-%d %H:%M:%S"), entry.peer or "—", _display(entry.unit_id),
            f"0x{entry.function:02X}" if entry.function is not None else "—", _display(entry.offset),
            _display(entry.count), entry.result)


class _EditDialog(QDialog):
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


class ServerWindow(QMainWindow):
    def __init__(self, bridge: IoBridge, storage: ServerStorage, return_home):
        super().__init__()
        self.setWindowTitle("工业协议调试台 · Modbus TCP 服务端")
        self.resize(1120, 800)
        self.setMinimumWidth(820)
        self._bridge = bridge
        self._storage = storage
        self._return_home = return_home
        self._state = ListenerState.STOPPED
        self._closing = False
        self._stopped = False
        self._going_home = False
        self._current = {}
        self._point_rows = {}
        self._stations = ()
        self._saved_plan = ServerPlan()
        self._load_error = False

        self._build()
        self._load()
        self._bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        self._bridge.stopped.connect(self._io_stopped, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _build(self):
        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        home = QPushButton("返回首页")
        home.clicked.connect(self._go_home)
        title = QLabel("Modbus TCP 服务端")
        title.setObjectName("title")
        self.status = QLabel()
        self.status.setObjectName("connectionStatus")
        header.addWidget(home)
        header.addWidget(title, 1)
        header.addWidget(self.status)
        layout.addWidget(masthead)

        body = QVBoxLayout()
        body.setContentsMargins(16, 6, 16, 12)
        layout.addLayout(body, 1)
        settings, settings_layout = _panel()
        controls = QHBoxLayout()
        self.host = QLineEdit()
        self.port = spinbox(1, 65535, 1502)
        for field in (self.host, self.port):
            field.setFont(QFont("Consolas"))
        self.listen = QPushButton("启动服务")
        self.listen.setObjectName("primary")
        self.listen.clicked.connect(self._toggle_listener)
        self.save = QPushButton("保存方案")
        self.save.clicked.connect(self._save_plan)
        self.dirty = QLabel()
        add_field(controls, "监听地址", self.host, 1)
        add_field(controls, "端口", self.port)
        controls.addWidget(self.listen)
        controls.addStretch()
        controls.addWidget(self.dirty)
        controls.addWidget(self.save)
        settings_layout.addLayout(controls)
        self.feedback = QLabel()
        self.feedback.setObjectName("warning")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        settings_layout.addWidget(self.feedback)
        body.addWidget(settings)
        self.host.textChanged.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)

        self.station_list = QListWidget()
        self.station_list.setAccessibleName("从站")
        station_panel, station_layout = _panel()
        station_layout.addWidget(_heading("从站"))
        station_layout.addWidget(self.station_list)
        station_panel.setMinimumWidth(232)
        station_actions = QHBoxLayout()
        self.add_station = self._button(station_actions, "新增", lambda: self._edit_station(True))
        self.edit_station = self._button(station_actions, "编辑", lambda: self._edit_station(False))
        self.remove_station = self._button(station_actions, "删除", self._delete_station)
        station_layout.addLayout(station_actions)

        point_panel, point_layout = _panel()
        point_layout.addWidget(_heading("点位 · 0 基偏移"))
        self.area_tabs = QTabBar()
        for area in Area:
            self.area_tabs.addTab(area.value)
            self.area_tabs.setTabData(self.area_tabs.count() - 1, area.value)
        point_layout.addWidget(self.area_tabs)
        self.points = Rows(("偏移", "名称", "类型", "初始值", "当前值"), self._point_row, (0, 3, 4), (0, 3, 4))
        self.point_table = _table(self.points)
        self.point_table.setAccessibleName("点位")
        point_layout.addWidget(self.point_table, 1)
        point_actions = QHBoxLayout()
        self.add_point = self._button(point_actions, "添加点位", lambda: self._edit_point("add"))
        self.batch_points = self._button(point_actions, "批量添加", lambda: self._edit_point("batch"))
        self.edit_point = self._button(point_actions, "编辑", lambda: self._edit_point("edit"))
        self.remove_point = self._button(point_actions, "删除", self._delete_point)
        point_actions.addStretch()
        self.edit_value = self._button(point_actions, "修改当前值", self._edit_current)
        point_layout.addLayout(point_actions)
        config = QSplitter(Qt.Orientation.Horizontal)
        config.addWidget(station_panel)
        config.addWidget(point_panel)
        config.setSizes([240, 820])
        config.setChildrenCollapsible(False)

        log_panel, log_layout = _panel()
        log_layout.addWidget(_heading("请求日志"))
        self.requests = Rows(("时间", "来源客户端", "Unit ID", "功能码", "起始偏移", "数量", "结果"),
                             _request_row, (2, 3, 4, 5), (1, 2, 3, 4, 5))
        request_table = _table(self.requests)
        request_table.setAccessibleName("请求日志")
        log_layout.addWidget(request_table, 1)
        self.detail = QPlainTextEdit("—")
        self.detail.setReadOnly(True)
        self.detail.setObjectName("packetDetail")
        self.detail.setAccessibleName("RX / TX 报文详情")
        request_table.selectionModel().currentRowChanged.connect(self._show_request)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(config)
        splitter.addWidget(log_panel)
        splitter.addWidget(self.detail)
        splitter.setSizes([400, 180, 110])
        body.addWidget(splitter, 1)
        self.station_list.currentRowChanged.connect(self._show_points)
        self.area_tabs.currentChanged.connect(self._show_points)
        self.point_table.selectionModel().currentRowChanged.connect(self._refresh)

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
            self.feedback.setText(f"无法读取方案 {self._storage.plan_path} · {error}")
        self._stations = self._saved_plan.stations
        self.host.setText(self._saved_plan.host)
        self.port.setValue(self._saved_plan.port)
        self._show_stations(0)
        try:
            self.requests.reset(self._storage.recent_requests())
        except OSError as error:
            self.feedback.setText(f"{self.feedback.text()}\n无法读取请求日志 · {error}".strip())

    def _plan(self):
        return ServerPlan(self.host.text().strip(), self.port.value(), self._stations)

    def _save_plan(self):
        plan = self._plan()
        try:
            self._storage.save_plan(plan)
        except (OSError, ValueError) as error:
            self.feedback.setText(f"保存方案失败 · {error}")
            return False
        self._saved_plan = plan
        self._load_error = False
        self.feedback.clear()
        self._refresh()
        return True

    @property
    def _station(self):
        row = self.station_list.currentRow()
        return self._stations[row] if 0 <= row < len(self._stations) else None

    @property
    def _area(self):
        return Area(self.area_tabs.tabData(self.area_tabs.currentIndex()))

    @property
    def _point(self):
        index = self.point_table.currentIndex()
        return self.points.items[index.row()] if index.isValid() else None

    def _show_stations(self, row):
        self.station_list.blockSignals(True)
        self.station_list.clear()
        for station in self._stations:
            label = f"Unit {station.unit_id}  ·  {station.name}" if station.name else f"Unit {station.unit_id}"
            self.station_list.addItem(label)
        self.station_list.setCurrentRow(min(row, len(self._stations) - 1))
        self.station_list.blockSignals(False)
        self._show_points()

    def _show_points(self, *_):
        station = self._station
        points = tuple(sorted((p for p in station.points if p.area == self._area),
                              key=lambda point: point.offset)) if station else ()
        self._point_rows = {point.offset: row for row, point in enumerate(points)}
        self.points.reset(points)
        self._refresh()

    def _point_row(self, point):
        key = self._station.unit_id, point.area, point.offset
        running = self._state in (ListenerState.LISTENING, ListenerState.STOPPING)
        current = self._current.get(key, point.initial) if running else None
        return (str(point.offset), point.name or "—", _point_type(point.area),
                _value_text(point.area, point.initial), _value_text(point.area, current))

    def _edit_station(self, adding):
        original = None if adding else self._station
        used = {station.unit_id for station in self._stations if station is not original}
        default = original.unit_id if original else next((unit for unit in range(1, 256) if unit not in used), 1)
        unit_id = spinbox(1, 255, default)
        name = QLineEdit(original.name if original else "")

        def commit():
            if unit_id.value() in used:
                raise ValueError(f"Unit ID {unit_id.value()} 已存在")
            row = len(self._stations) if adding else self.station_list.currentRow()
            station = Station(unit_id.value(), name.text().strip(), original.points if original else ())
            self._stations = self._stations[:row] + (station,) + self._stations[row + 1:]
            self._show_stations(row)

        dialog = _EditDialog(self, "新增从站" if adding else "编辑从站", commit)
        dialog.form.addRow("Unit ID", unit_id)
        dialog.form.addRow("名称（可选）", name)
        dialog.exec()

    def _delete_station(self):
        station = self._station
        message = f"删除 Unit {station.unit_id} 及其 {len(station.points)} 个点位？"
        if QMessageBox.question(self, "删除从站", message) != QMessageBox.StandardButton.Yes:
            return
        row = self.station_list.currentRow()
        self._stations = self._stations[:row] + self._stations[row + 1:]
        self._show_stations(row)

    def _replace_station(self, station):
        row = self.station_list.currentRow()
        self._stations = self._stations[:row] + (station,) + self._stations[row + 1:]
        self._show_points()

    def _edit_point(self, mode):
        station, area = self._station, self._area
        original = self._point if mode == "edit" else None
        batch = mode == "batch"
        offset = spinbox(0, 65535, original.offset if original else 0)
        count = spinbox(1, 65536, 1)
        name = QLineEdit(original.name if original else "")
        initial, get_initial = _value_field(area, original.initial if original else 0)

        def commit():
            points = tuple(point for point in station.points if point is not original)
            updated = replace(station, points=points).add_points(
                area, offset.value(), count.value(), get_initial(), "" if batch else name.text().strip())
            if original is not None:
                edited = updated.points[-1]
                updated = replace(updated, points=tuple(edited if p is original else p for p in station.points))
            self._replace_station(updated)
            self.point_table.selectRow(self._point_rows[offset.value()])

        dialog = _EditDialog(self, {"add": "新增点位", "batch": "批量添加点位", "edit": "编辑点位"}[mode], commit)
        dialog.form.addRow("从站 / 数据区", QLabel(f"Unit {station.unit_id}  ·  {RULES[area].label} {area}"))
        dialog.form.addRow("类型 / 值范围", QLabel("Bool · 开或关" if _point_type(area) == "Bool" else "UInt16 · 0–65535"))
        dialog.form.addRow("起始偏移（0 基）" if batch else "偏移（0 基）", offset)
        if batch:
            preview = QLabel()

            def show_range():
                preview.setText(f"{offset.value()}–{offset.value() + count.value() - 1}")

            dialog.form.addRow("数量", count)
            dialog.form.addRow("占用区间", preview)
            offset.valueChanged.connect(show_range)
            count.valueChanged.connect(show_range)
            show_range()
        else:
            dialog.form.addRow("名称（可选）", name)
        dialog.form.addRow("初始值", initial)
        dialog.exec()

    def _delete_point(self):
        station, point = self._station, self._point
        self._replace_station(replace(station, points=tuple(p for p in station.points if p is not point)))

    def _edit_current(self):
        point, station = self._point, self._station
        key = station.unit_id, point.area, point.offset
        field, get_value = _value_field(point.area, self._current.get(key, point.initial))

        def commit():
            self._bridge.submit(self._bridge.session.set_current(*key, get_value()))

        dialog = _EditDialog(self, "修改当前值", commit)
        dialog.form.addRow("点位", QLabel(f"Unit {station.unit_id}  ·  {point.area} / {point.offset}"))
        dialog.form.addRow("当前值", field)
        dialog.exec()

    def _toggle_listener(self):
        self.feedback.clear()
        session = self._bridge.session
        if self._state == ListenerState.STOPPED:
            plan = self._plan()
            self._state = ListenerState.STARTING
            self._bridge.submit(session.start(plan))
        elif self._state == ListenerState.LISTENING:
            self._state = ListenerState.STOPPING
            self._bridge.submit(session.stop())
        self._refresh()

    def _handle_event(self, event):
        if isinstance(event, ListenerUpdate):
            self._state = event.state
            if event.error:
                self.feedback.setText(event.error)
            if event.state == ListenerState.STOPPED:
                self._current.clear()
            if event.state in (ListenerState.LISTENING, ListenerState.STOPPED):
                self._update_point_cells(0, len(self.points.items) - 1)
            self._refresh()
        elif isinstance(event, ValuesChanged):
            for offset, value in enumerate(event.values, event.offset):
                self._current[event.unit_id, event.area, offset] = value
            if self._station and self._station.unit_id == event.unit_id and self._area == event.area:
                self._update_point_cells(self._point_rows[event.offset],
                                         self._point_rows[event.offset + len(event.values) - 1])
        elif isinstance(event, ServerRequest):
            try:
                if self._storage.append_request(event):
                    self.requests.reset()
                    self.detail.setPlainText("—")
            except OSError as error:
                self.feedback.setText(f"保存请求日志失败 · {error}")
            self.requests.prepend(event)

    def _update_point_cells(self, first, last):
        if last >= first:
            self.points.dataChanged.emit(self.points.index(first, 4), self.points.index(last, 4))

    def _show_request(self, index, _previous):
        if index.isValid():
            entry = self.requests.items[index.row()]
            self.detail.setPlainText(f"RX  {entry.rx.hex(' ').upper() or '—'}\nTX  {entry.tx.hex(' ').upper() or '—'}")
        else:
            self.detail.setPlainText("—")

    def _refresh(self, *_):
        idle = self._state == ListenerState.STOPPED and not self._closing
        running = self._state == ListenerState.LISTENING and not self._closing
        self.status.setText(self._state)
        tone = "online" if running else "offline" if idle else "pending"
        if self.status.property("tone") != tone:
            self.status.setProperty("tone", tone)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
        self.listen.setText("停止服务" if running or self._state == ListenerState.STOPPING else "启动服务")
        self.listen.setEnabled(idle or running)
        for control in (self.host, self.port, self.add_station):
            control.setEnabled(idle)
        for control in (self.edit_station, self.remove_station, self.add_point, self.batch_points):
            control.setEnabled(idle and self._station is not None)
        for control in (self.edit_point, self.remove_point):
            control.setEnabled(idle and self._point is not None)
        self.edit_value.setEnabled(running and self._point is not None)
        self.save.setEnabled(not self._closing)
        self.dirty.setText("有未保存修改" if self._plan() != self._saved_plan else
                           "方案未加载" if self._load_error else "已保存")

    def _go_home(self):
        self._going_home = True
        if not self.close() and not self._closing:
            self._going_home = False

    def _io_stopped(self):
        self._stopped = True
        if self._going_home:
            self._return_home()
        self.close()

    def closeEvent(self, event):
        if self._stopped:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if self._state in (ListenerState.STARTING, ListenerState.LISTENING):
            if QMessageBox.question(self, "停止服务", "停止服务并离开工作台？") != QMessageBox.StandardButton.Yes:
                return
        if self._plan() != self._saved_plan:
            prompt = QMessageBox(QMessageBox.Icon.Question, "未保存的方案", "离开前如何处理方案修改？", parent=self)
            save = prompt.addButton("保存并离开", QMessageBox.ButtonRole.AcceptRole)
            discard = prompt.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
            cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            prompt.setDefaultButton(cancel)
            prompt.exec()
            choice = prompt.clickedButton()
            if choice != discard and (choice != save or not self._save_plan()):
                return
        self._closing = True
        self._refresh()
        self._bridge.shutdown()
