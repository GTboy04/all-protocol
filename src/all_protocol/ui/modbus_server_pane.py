from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QSplitter, QTabBar, QVBoxLayout,
)

from ..adapters.modbus_tcp.model import RULES, Area, data_types
from ..adapters.modbus_tcp.server import ValuesChanged
from ..adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, Station
from ..adapters.modbus_tcp.values import DataType, RegisterOrder
from ..application.server_contracts import ListenerState, RequestMetadata, ServerEvent
from .main_window import Rows, _heading, _panel, _table
from .modbus_tcp_form import order_picker
from .server_pane import EditDialog, ServerPane
from .widgets import spinbox


def _display(value):
    return "—" if value is None else str(value)


def _value_text(data_type, value):
    if value is not None and data_type == DataType.BOOL:
        return "开" if value else "关"
    return _display(value)


def _value_field(data_type, value, parse=None):
    if data_type == DataType.BOOL:
        field = QComboBox()
        field.addItems(("关", "开"))
        field.setCurrentIndex(value)
        return field, field.currentIndex
    field = QLineEdit(str(int(value)) if isinstance(value, bool) else str(value))
    read = parse if parse is not None else data_type.parse
    return field, lambda: read(field.text())


def _request_row(event: ServerEvent):
    time = event.time.strftime("%m-%d %H:%M:%S")
    entry = event.payload
    if not isinstance(entry, RequestMetadata):
        return (time, "—", "—", "—", "—", "—", entry.result)
    details = dict(entry.details)
    function = details.get("function")
    return (time, entry.peer or "—", _display(details.get("unit_id")),
            f"0x{function:02X}" if type(function) is int else "—", _display(details.get("offset")),
            _display(details.get("count")), entry.result)


class ModbusServerPane(ServerPane):
    def __init__(self, bridge, storage):
        super().__init__(bridge, storage, ServerPlan())
        self._current = {}
        self._point_rows = {}
        self._stations = ()
        self._build()
        self._load()

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(16, 10, 16, 12)
        body.addWidget(self._settings())

        self.station_list = QListWidget()
        self.station_list.setAccessibleName("从站")
        self.station_list.setObjectName("serverStations")
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
        self.point_table.horizontalHeader().setStretchLastSection(False)
        self.point_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
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

        journal = self._journal_panel(
            ("时间", "来源客户端", "Unit ID", "功能码", "偏移", "数量", "结果"),
            _request_row, (2, 3, 4, 5), (1, 2, 3, 4, 5),
        )
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(config)
        splitter.addWidget(journal)
        splitter.setSizes([400, 240])
        body.addWidget(splitter, 1)
        self.station_list.currentRowChanged.connect(self._show_points)
        self.area_tabs.currentChanged.connect(self._show_points)
        self.point_table.selectionModel().currentRowChanged.connect(self._refresh)

    def _load_points(self, plan):
        self._stations = plan.stations
        self._show_stations(0)

    def _plan(self):
        return ServerPlan(self.host.text().strip(), self.port.value(), self._stations)

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
        self._point_rows = {address: row for row, point in enumerate(points) for address in point.addresses}
        self.points.reset(points)
        self._refresh()

    def _point_row(self, point):
        running = self.state in (ListenerState.LISTENING, ListenerState.STOPPING)
        current = self._current_value(point) if running else None
        address = str(point.offset) if len(point.addresses) == 1 else f"{point.offset}–{point.addresses[-1]}"
        return (address, point.name or "—", str(point.value_type),
                _value_text(point.value_type, point.initial), _value_text(point.value_type, current))

    def _current_value(self, point):
        defaults = point.encode(point.initial)
        unit = self._station.unit_id
        words = tuple(self._current.get((unit, point.area, address), word)
                      for address, word in zip(point.addresses, defaults, strict=True))
        return point.decode(words)

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

        dialog = EditDialog(self, "新增从站" if adding else "编辑从站", commit)
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
        types = QComboBox()
        for data_type in data_types(area):
            types.addItem(data_type.value, data_type.value)
        if original:
            types.setCurrentIndex(types.findData(original.value_type.value))
        order = order_picker(original.order if original else RegisterOrder.BIG)

        def selected_type():
            return DataType(types.currentData())

        initial, get_initial = _value_field(
            selected_type(), original.initial if original else 0, lambda text: selected_type().parse(text))

        def commit():
            points = tuple(point for point in station.points if point is not original)
            updated = replace(station, points=points).add_points(
                area, offset.value(), count.value(), get_initial(), "" if batch else name.text().strip(),
                selected_type(), RegisterOrder(order.currentData()))
            if original is not None:
                edited = updated.points[-1]
                updated = replace(updated, points=tuple(edited if p is original else p for p in station.points))
            self._replace_station(updated)
            self.point_table.selectRow(self._point_rows[offset.value()])

        dialog = EditDialog(self, {"add": "新增点位", "batch": "批量添加点位", "edit": "编辑点位"}[mode], commit)
        dialog.form.addRow("从站 / 数据区", QLabel(f"Unit {station.unit_id}  ·  {RULES[area].label} {area}"))
        dialog.form.addRow("数据类型", types)
        if selected_type() != DataType.BOOL:
            dialog.form.addRow("寄存器顺序", order)
        dialog.form.addRow("起始偏移（0 基）" if batch else "偏移（0 基）", offset)
        if batch:
            dialog.form.addRow("点位数量", count)
        else:
            dialog.form.addRow("名称（可选）", name)
        preview = QLabel()

        def show_range():
            end = offset.value() + count.value() * selected_type().words - 1
            preview.setText(str(end) if end == offset.value() else f"{offset.value()}–{end}")

        dialog.form.addRow("占用地址", preview)
        for field in (offset, count):
            field.valueChanged.connect(show_range)
        types.currentIndexChanged.connect(show_range)
        show_range()
        dialog.form.addRow("初始值", initial)
        dialog.exec()

    def _delete_point(self):
        station, point = self._station, self._point
        self._replace_station(replace(station, points=tuple(p for p in station.points if p is not point)))

    def _edit_current(self):
        point, station = self._point, self._station
        key = station.unit_id, point.area, point.offset
        field, get_value = _value_field(point.value_type, self._current_value(point))

        def commit():
            value = get_value()
            point.encode(value)
            self.bridge.submit(self.bridge.session.edit_current(ModbusServerTarget(*key), value))

        dialog = EditDialog(self, "修改当前值", commit)
        dialog.form.addRow("点位", QLabel(f"Unit {station.unit_id}  ·  {point.area} / {point.offset}"))
        dialog.form.addRow("当前值", field)
        dialog.exec()

    def _state_changed(self):
        if self.state == ListenerState.STOPPED:
            self._current.clear()
        if self.state in (ListenerState.LISTENING, ListenerState.STOPPED):
            self._update_point_cells(0, len(self.points.items) - 1)

    def _runtime_changed(self, event):
        if isinstance(event, ValuesChanged):
            for offset, value in enumerate(event.values, event.offset):
                self._current[event.unit_id, event.area, offset] = value
            if self._station and self._station.unit_id == event.unit_id and self._area == event.area:
                self._update_point_cells(self._point_rows[event.offset],
                                         self._point_rows[event.offset + len(event.values) - 1])

    def _update_point_cells(self, first, last):
        if last >= first:
            self.points.dataChanged.emit(self.points.index(first, 4), self.points.index(last, 4))

    def _refresh(self, *_):
        idle, running = self._refresh_listener()
        self.add_station.setEnabled(idle)
        for control in (self.edit_station, self.remove_station, self.add_point, self.batch_points):
            control.setEnabled(idle and self._station is not None)
        for control in (self.edit_point, self.remove_point):
            control.setEnabled(idle and self._point is not None)
        self.edit_value.setEnabled(running and self._point is not None)
