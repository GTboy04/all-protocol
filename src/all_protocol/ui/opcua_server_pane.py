from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from ..adapters.opcua.model import UaTarget, UaType
from ..adapters.opcua.server_model import SIMULATOR_NAMESPACE, UaServerPlan, UaServerPoint
from ..application.contracts import ClientError
from ..application.server_contracts import ListenerState, RequestMetadata
from .main_window import Rows, _panel, _table
from .opcua_form import _Query
from .server_pane import ServerPane
from ..application.binary import bit_pattern
from .bit_inspector import BitReading, show_bit_inspector
from .bit_definitions_editor import BitDefinitionsEditor
from .widgets import (EditDialog, add_field, configure_point_reordering,
                      reordered_points, set_point_reordering_enabled)


def _request_row(event):
    payload = event.payload
    if not isinstance(payload, RequestMetadata):
        return event.time.strftime("%H:%M:%S"), "—", "—", payload.result
    fields = dict(payload.details)
    return event.time.strftime("%H:%M:%S"), fields.get("service", "—"), fields.get("target", "—"), payload.result


class UaServerPane(ServerPane):
    def __init__(self, bridge, storage):
        super().__init__(bridge, storage, UaServerPlan())
        self._points, self._values = (), {}
        body = QVBoxLayout(self)
        body.addWidget(self._settings())
        panel, layout = _panel()
        options = QHBoxLayout()
        self.namespace = QLineEdit(SIMULATOR_NAMESPACE)
        self.security = QComboBox()
        self.security.addItem("请选择模拟端安全模式", "Disabled")
        self.security.addItem("仅本机无加密匿名模拟", "None/None")
        add_field(options, "稳定命名空间 URI", self.namespace, 1)
        add_field(options, "模拟模式", self.security)
        layout.addLayout(options)
        hint = QLabel("此模拟端仅监听本机回环地址。向局域网或真实设备连接时，请使用客户端的证书安全配置。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.points = Rows(("节点地址", "名称", "类型", "客户端权限", "初始值", "当前值"), self._point_row)
        self.point_table = _table(self.points)
        configure_point_reordering(self.point_table, self._move_point)
        self.point_table.setAccessibleName("OPC UA 模拟节点")
        layout.addWidget(self.point_table, 1)
        actions = QHBoxLayout()
        self.add_point = self._button(actions, "添加节点", lambda: self._edit_point(None))
        self.edit_point = self._button(actions, "编辑节点", lambda: self._edit_point(self._point))
        self.remove_point = self._button(actions, "删除节点", self._delete_point)
        actions.addStretch()
        self.bit_button = self._button(actions, "位查看…", lambda: show_bit_inspector(self, self._bit_reading))
        self.edit_value = self._button(actions, "修改当前值", self._edit_current)
        layout.addLayout(actions)
        self.plan_controls = (self.save, self.add_point, self.edit_point, self.remove_point)
        body.addWidget(panel, 2)
        body.addWidget(self._journal_panel(("时间", "服务", "节点", "结果"), _request_row), 1)
        self.point_table.selectionModel().currentRowChanged.connect(self._refresh)
        self.namespace.textChanged.connect(self._refresh)
        self.security.currentIndexChanged.connect(self._refresh)
        self._load()

    @property
    def configured_endpoint(self):
        return self._plan().endpoint

    @property
    def _point(self):
        index = self.point_table.currentIndex()
        return self.points.items[index.row()] if index.isValid() else None

    def _plan(self):
        return UaServerPlan(self.host.text().strip(), self.port.value(), self._points,
                            self.security.currentData(), self.namespace.text().strip())

    def _load(self):
        try:
            self._saved_plan = self._storage.load_plan()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._load_error = True
            self._feedback(str(error))
        plan = self._saved_plan
        self.host.setText(plan.host)
        self.port.setValue(plan.port)
        self.namespace.setText(plan.namespace_uri)
        self.security.setCurrentIndex(self.security.findData(plan.security))
        self._points = plan.points
        self.points.reset(self._points)
        try:
            self.requests.reset(self._storage.recent_events())
        except OSError as error:
            self._feedback(str(error))
        self.bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _point_row(self, point):
        return (point.target.label, point.name, point.target.data_type, "可读写" if point.writable else "只读",
                str(point.initial), str(self._values.get(point.target, "—")))

    def _bit_reading(self):
        point = self._point
        if point is None:
            return None
        running = self.state in (ListenerState.LISTENING, ListenerState.STOPPING)
        value = self._values.get(point.target) if running else point.initial
        return BitReading(point.name or point.target.label, bit_pattern(value, str(point.target.data_type)),
                          "当前值" if running else "配置初始值（服务已停止）", point.bit_definitions,
                          str(point.target.data_type))

    def _edit_point(self, original):
        query = _Query()
        query.nodes.hide()
        query.browse.hide()
        query.root.hide()
        query.show_nodes.hide()
        query.hint.hide()
        target = original.target if original else UaTarget(self.namespace.text().strip(), "s", "Counter", UaType.Int32)
        query.set_target(target)
        bit_definitions = BitDefinitionsEditor(lambda: str(query.target().data_type), original.bit_definitions if original else ())
        query.namespace_uri.setReadOnly(True)
        name = QLineEdit(original.name if original else "")
        value = QLineEdit(str(original.initial) if original else "0")
        permission = QComboBox()
        permission.addItem("可读写", True)
        permission.addItem("只读", False)
        permission.setCurrentIndex(0 if original is None or original.writable else 1)

        def commit():
            selected = query.target()
            try:
                initial = selected.data_type.parse(value.text())
                point = UaServerPoint(selected, selected.prepare_write((initial,))[0], name.text(), permission.currentData(),
                                      original.simulation if original else None, bit_definitions.definitions)
                points = tuple(point if item is original else item for item in self._points) if original else self._points + (point,)
                replace(self._plan(), points=points).validate()
            except ClientError as error:
                raise ValueError(str(error)) from None
            self._points = points
            self.points.reset(points)
            self.point_table.selectRow(self.points.items.index(point))
            self._refresh()

        dialog = EditDialog(self, "编辑 OPC UA 节点" if original else "添加 OPC UA 节点", commit)
        for label, field in (("节点", query), ("名称", name), ("初始值", value), ("客户端权限", permission),
                             ("逐位定义", bit_definitions)):
            dialog.form.addRow(label, field)
        dialog.resize(900, 300)
        dialog.exec()
        dialog.deleteLater()

    def _move_point(self, source, destination):
        if not self.point_table.verticalHeader().sectionsMovable():
            return
        points = reordered_points(self._points, self.points.items, source, destination)
        if points == self._points:
            return
        selected = self.points.items[source]
        self._points = points
        self.points.reset(points)
        self.point_table.selectRow(self.points.items.index(selected))
        self._refresh()

    def _delete_point(self):
        self._points = tuple(item for item in self._points if item is not self._point)
        self.points.reset(self._points)
        self._refresh()

    def _edit_current(self):
        point = self._point
        field = QLineEdit(str(self._values.get(point.target, point.initial)))

        def commit():
            try:
                value = point.encode(point.target.data_type.parse(field.text()))
            except ClientError as error:
                raise ValueError(str(error)) from None
            self.bridge.submit(self.bridge.session.edit_current(point.target, value))

        dialog = EditDialog(self, "修改 OPC UA 当前值", commit)
        dialog.form.addRow(point.name or point.target.identifier, field)
        dialog.exec()
        dialog.deleteLater()

    def set_simulation_rule(self, target, rule):
        self._points = tuple(replace(point, simulation=rule) if point.target == target else point for point in self._points)
        self.points.reset(self._points)
        self._refresh()

    def _state_changed(self):
        self._values = {point.target: point.initial for point in self._points} if self.state == ListenerState.LISTENING else {}
        self._update_values()

    def _runtime_changed(self, details):
        self._values.update(details.values)
        self._update_values()

    def _update_values(self):
        if self._points:
            self.points.dataChanged.emit(self.points.index(0, 5), self.points.index(len(self._points) - 1, 5))

    def _refresh_fault(self):
        self.fault_button.hide()
        self.reset_fault.hide()
        self.fault_status.setText("OPC UA 模拟端记录服务与状态码；首版不提供故障注入。")

    def _refresh(self, *_):
        if not hasattr(self, "security"):
            return
        idle, running = self._refresh_listener()
        set_point_reordering_enabled(self.point_table, idle, "停止服务后，可拖动左侧行号排序。")
        self.namespace.setEnabled(idle and not self._points)
        self.security.setEnabled(idle)
        self.add_point.setEnabled(idle)
        self.edit_point.setEnabled(idle and self._point is not None)
        self.remove_point.setEnabled(idle and self._point is not None)
        self.edit_value.setEnabled(running and self._point is not None)
        self.scene_button.setEnabled(True)
