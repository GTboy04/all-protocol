from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QSplitter, QVBoxLayout

from ..adapters.s7.model import MAX_OFFSET, Area, DataType, S7Target
from ..adapters.s7.server_model import S7Memory, S7ServerPlan, S7ServerPoint, S7ValuesChanged
from ..application.server_contracts import ListenerState, RequestMetadata, ServerEvent
from .main_window import Rows, _heading, _panel, _table
from .server_pane import ServerPane
from .widgets import EditDialog, spinbox
from ..application.binary import bit_pattern
from .bit_inspector import BitReading, show_bit_inspector


AREAS = ((Area.DB, "DB 数据块"), (Area.M, "M 标志"), (Area.I, "I 输入"), (Area.Q, "Q 输出"))


def _request_row(event: ServerEvent):
    time = event.time.strftime("%m-%d %H:%M:%S")
    entry = event.payload
    if not isinstance(entry, RequestMetadata):
        return (time, "—", "—", "—", entry.result)
    details = dict(entry.details)
    function = details.get("function")
    return (time, entry.peer or "—", f"0x{function:02X}" if type(function) is int else "—",
            details.get("target") or "—", entry.result)


def _value_text(value):
    if value is None:
        return "—"
    return str(int(value)) if isinstance(value, bool) else str(value)


def _parse_value(data_type, text):
    try:
        return float(text) if data_type == DataType.REAL else int(text)
    except ValueError as error:
        raise ValueError("请输入有效的十进制数值，Bool 使用 0 或 1") from error


class S7ServerPane(ServerPane):
    def __init__(self, bridge, storage):
        super().__init__(bridge, storage, S7ServerPlan())
        self._points = ()
        self._memory = None
        self._build()
        self._load()

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(16, 10, 16, 12)
        body.addWidget(self._settings())
        panel, layout = _panel()
        toolbar = QHBoxLayout()
        toolbar.addWidget(_heading("PLC 点位"))
        self.area_filter = QComboBox()
        self.area_filter.setAccessibleName("筛选数据区")
        self.area_filter.addItem("全部数据区", None)
        for area, caption in AREAS:
            self.area_filter.addItem(caption, area.value)
        toolbar.addWidget(self.area_filter)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.points = Rows(("绝对地址", "名称", "类型", "初始值", "当前值"), self._point_row, (3, 4), (0, 3, 4))
        self.point_table = _table(self.points)
        self.point_table.setAccessibleName("S7 点位")
        self.point_table.horizontalHeader().setStretchLastSection(False)
        self.point_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.point_table, 1)
        actions = QHBoxLayout()
        self.add_point = self._button(actions, "添加点位", lambda: self._edit_point(None))
        self.edit_point = self._button(actions, "编辑", lambda: self._edit_point(self._point))
        self.remove_point = self._button(actions, "删除", self._delete_point)
        actions.addStretch()
        self.bit_button = self._button(actions, "位查看…", lambda: show_bit_inspector(self, self._bit_reading))
        self.edit_value = self._button(actions, "修改当前值", self._edit_current)
        self.plan_controls = (self.save, self.add_point, self.edit_point, self.remove_point)
        layout.addLayout(actions)
        journal = self._journal_panel(("时间", "来源客户端", "功能码", "目标", "结果"), _request_row,
                                      monospace=(1, 2, 3))
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(panel)
        splitter.addWidget(journal)
        splitter.setSizes([400, 240])
        body.addWidget(splitter, 1)
        self.point_table.selectionModel().currentRowChanged.connect(self._refresh)
        self.area_filter.currentIndexChanged.connect(self._show_points)

    def _load_points(self, plan):
        self._points = plan.points
        self._show_points()

    def _plan(self):
        return S7ServerPlan(self.host.text().strip(), self.port.value(), self._points)

    def set_simulation_rule(self, target, rule):
        index = self.point_table.currentIndex()
        self._points = tuple(replace(point, simulation=rule) if point.target == target else point
                             for point in self._points)
        self._show_points()
        if index.isValid():
            self.point_table.selectRow(index.row())

    @property
    def _point(self):
        index = self.point_table.currentIndex()
        return self.points.items[index.row()] if index.isValid() else None

    def _show_points(self, *_):
        area = self.area_filter.currentData()
        visible = (point for point in self._points if area is None or point.target.area == area)
        self.points.reset(sorted(visible, key=lambda point: (
            point.target.area != Area.DB, point.target.area, point.target.db, point.target.offset, point.target.bit,
        )))
        self._refresh()

    def _point_row(self, point):
        target = point.target
        current = self._memory.current(point) if self._memory is not None else None
        return (target.address(), point.name or "—", target.data_type, _value_text(point.initial), _value_text(current))

    def _bit_reading(self):
        point = self._point
        if point is None:
            return None
        running = self.state in (ListenerState.LISTENING, ListenerState.STOPPING)
        value = self._memory.current(point) if running and self._memory is not None else point.initial
        label = f"{point.target.address()} · {point.name}"
        return BitReading(label, bit_pattern(value, str(point.target.data_type)),
                          f"{'当前值' if running else '配置初始值（服务已停止）'}：{value}")

    def _edit_point(self, original):
        default_area = Area(self.area_filter.currentData() or Area.DB)
        target = original.target if original else S7Target(default_area, int(default_area == Area.DB), 0, DataType.WORD)
        area = QComboBox()
        for value, caption in AREAS:
            area.addItem(caption, value.value)
        area.setCurrentIndex(area.findData(target.area.value))
        db = spinbox(1, 65535, target.db or 1)
        data_type = QComboBox()
        for value in DataType:
            data_type.addItem(value.value, value.value)
        data_type.setCurrentIndex(data_type.findData(target.data_type.value))
        offset = spinbox(0, MAX_OFFSET, target.offset)
        bit = spinbox(0, 7, target.bit)
        name = QLineEdit(original.name if original else "")
        initial = QLineEdit(_value_text(original.initial) if original else "0")
        address = QLabel()

        def selected():
            data_area = Area(area.currentData())
            kind = DataType(data_type.currentData())
            return S7Target(data_area, db.value() if data_area == Area.DB else 0, offset.value(), kind,
                            bit=bit.value() if kind == DataType.BOOL else 0)

        def refresh():
            selection = selected()
            db.setEnabled(selection.area == Area.DB)
            bit.setEnabled(selection.data_type == DataType.BOOL)
            offset.setMaximum(MAX_OFFSET - selection.byte_count + 1)
            address.setText(selected().address())

        def commit():
            selection = selected()
            point = S7ServerPoint(selection, _parse_value(selection.data_type, initial.text()), name.text().strip(),
                                  original.simulation if original else None)
            if original is None:
                points = self._points + (point,)
            else:
                points = tuple(point if other is original else other for other in self._points)
            replace(self._saved_plan, points=points).validate()
            self._points = points
            if self.area_filter.currentData() is not None:
                self.area_filter.setCurrentIndex(self.area_filter.findData(selection.area.value))
            self._show_points()
            self.point_table.selectRow(self.points.items.index(point))

        dialog = EditDialog(self, "编辑 S7 点位" if original else "添加 S7 点位", commit)
        for label, field in (
            ("数据区", area), ("DB 号", db), ("数据类型", data_type), ("字节偏移（0 基）", offset),
            ("位索引", bit), ("绝对地址", address), ("名称（可选）", name), ("初始值", initial),
        ):
            dialog.form.addRow(label, field)
        for picker in (area, data_type):
            picker.currentIndexChanged.connect(refresh)
        for field in (db, offset, bit):
            field.valueChanged.connect(refresh)
        refresh()
        dialog.exec()

    def _delete_point(self):
        point = self._point
        self._points = tuple(other for other in self._points if other is not point)
        self._show_points()

    def _edit_current(self):
        point = self._point
        field = QLineEdit(_value_text(self._memory.current(point)))

        def commit():
            value = _parse_value(point.target.data_type, field.text())
            point.encode(value)
            self.bridge.submit(self.bridge.session.edit_current(point.target, value))

        dialog = EditDialog(self, "修改当前值", commit)
        dialog.form.addRow("点位", QLabel(point.target.address()))
        dialog.form.addRow("当前值", field)
        dialog.exec()

    def _state_changed(self):
        if self.state == ListenerState.LISTENING and self._memory is None:
            self._memory = S7Memory(self._points)
        elif self.state == ListenerState.STOPPED:
            self._memory = None
        self._refresh_values()

    def _runtime_changed(self, event):
        if isinstance(event, S7ValuesChanged) and self._memory is not None:
            self._memory.apply(event)
            self._refresh_values()

    def _refresh_values(self):
        if self.points.items:
            self.points.dataChanged.emit(self.points.index(0, 4), self.points.index(len(self.points.items) - 1, 4))

    def _refresh(self, *_):
        idle, running = self._refresh_listener()
        selected = self._point is not None
        self.add_point.setEnabled(idle)
        self.edit_point.setEnabled(idle and selected)
        self.remove_point.setEnabled(idle and selected)
        self.edit_value.setEnabled(running and selected)
