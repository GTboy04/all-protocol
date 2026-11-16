from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..application.contracts import ClientError, ConnectionState, SessionState, TableReadUpdate
from ..application.sampling import Quality, SampleStore
from ..application.binary import binary_text, bit_pattern, target_kind
from .bit_inspector import BitReading, show_bit_inspector
from .bit_definitions_editor import BitDefinitionsEditor
from ..point_tables import (
    IMPORT_LIMIT_BYTES, PointDefinition, PointTable, export_points_csv, export_table_json,
    import_points_csv, import_table_json, new_id, target_record,
)
from ..storage import _replace_file
from .main_window import Rows, _table
from .widgets import EditDialog
from .widgets import configure_point_reordering, reordered_points, set_point_reordering_enabled, spinbox


def _definition_row(point):
    return (point.name, point.target.label, point.unit, str(point.scale), str(point.offset), point.description)


def _matches(point, query):
    text = "\n".join((point.name, point.target.label, point.description)).casefold()
    return all(word in text for word in query.casefold().split())


class PointDialog(EditDialog):
    def __init__(self, parent, device, create_form, original=None):
        super().__init__(parent, "编辑点位" if original else "新增点位", self._commit)
        self.definition = None
        self.original = original
        self.target_form = create_form()
        self.target_form.connection.setParent(self)
        self.target_form.connection.hide()
        fields = {**device.profile.fields, "count": 1}
        if original is not None:
            fields.update(target_record(original.target))
        self.target_form.restore_profile(fields)
        self.target_form.query.count.setValue(1)
        self.target_form.query.count.setEnabled(False)
        self.name = QLineEdit(original.name if original else "新点位")
        self.unit = QLineEdit(original.unit if original else "")
        self.scale = QLineEdit(str(original.scale) if original else "1")
        self.offset = QLineEdit(str(original.offset) if original else "0")
        self.precision = spinbox(0, 12, original.precision if original else 3)
        self.description = QPlainTextEdit(original.description if original else "")
        self.description.setAccessibleName("点位功能描述")
        self.description.setPlaceholderText("用途：控制或监测什么\n取值含义：例如 0=停止，1=运行\n操作注意事项：例如写入前确认设备已停机")
        self.description.setMinimumHeight(115)
        self.bit_definitions = BitDefinitionsEditor(lambda: target_kind(self.target_form.target()),
                                                   original.bit_definitions if original else ())
        for label, field in (("名称", self.name), ("协议地址", self.target_form.query), ("单位", self.unit),
                             ("倍率", self.scale), ("偏置", self.offset), ("小数位数", self.precision),
                             ("功能描述", self.description), ("逐位定义", self.bit_definitions)):
            self.form.addRow(label, field)
        self.protocol = device.profile.protocol
        self.resize(760, 560)

    def _commit(self):
        try:
            definition = PointDefinition(self.original.id if self.original else new_id(), self.name.text().strip(),
                                         self.target_form.target(), self.unit.text(), float(self.scale.text()),
                                         float(self.offset.text()), self.description.toPlainText(), self.precision.value(),
                                         self.bit_definitions.definitions)
            definition.validate(self.protocol)
        except ClientError as error:
            raise ValueError(str(error)) from error
        self.definition = definition


class ImportPreview(QDialog):
    def __init__(self, parent, table):
        super().__init__(parent)
        self.setWindowTitle("预览导入点表")
        self.resize(850, 440)
        layout = QVBoxLayout(self)
        title = QLabel(f"{table.name} · {len(table.points)} 个点位；确认后新增点表")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)
        self.rows = Rows(("名称", "地址", "单位", "倍率", "偏置", "说明"), _definition_row)
        self.rows.setParent(self)
        self.rows.reset(table.points)
        layout.addWidget(_table(self.rows))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入点表")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CsvEditDialog(QDialog):
    def __init__(self, parent, table, protocol, validate):
        super().__init__(parent)
        self.setWindowTitle("批量编辑点位")
        self.resize(900, 600)
        self.table, self.protocol, self.validate = table, protocol, validate
        self.points = None
        self.preview_text = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("address_offset 是协议地址，offset 是工程值偏置。保留 id 可保留点位身份。"))
        self.input = QPlainTextEdit(export_points_csv(table.points))
        self.input.setAccessibleName("点位 CSV")
        layout.addWidget(self.input, 1)
        preview = QPushButton("校验并预览")
        preview.clicked.connect(self._preview)
        layout.addWidget(preview)
        self.error = QLabel()
        self.error.setObjectName("warning")
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.rows = Rows(("名称", "地址", "单位", "倍率", "偏置", "说明"), _definition_row)
        self.rows.setParent(self)
        layout.addWidget(_table(self.rows), 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用整表修改")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.input.textChanged.connect(self._changed)

    def _changed(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.points, self.preview_text = None, None
        self.rows.reset()

    def _preview(self):
        try:
            contents = self.input.toPlainText()
            if len(contents.encode("utf-8")) > IMPORT_LIMIT_BYTES:
                raise ValueError("CSV 超过 10 MiB")
            points = import_points_csv(contents, self.protocol)
            self.validate(replace(self.table, points=points))
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.error.clear()
        self.points, self.preview_text = points, contents
        self.rows.reset(points)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def accept(self):
        if self.points is not None and self.preview_text == self.input.toPlainText():
            super().accept()


class PointTablePane(QWidget):
    workspace_changed = Signal()

    def __init__(self, workspace, device, create_form, save_device, read_table, write_point, can_write,
                 poll_table, stop_all, *, subscribe_table=None, embedded=False):
        super().__init__()
        self.workspace, self.device, self.create_form = workspace, device, create_form
        self.save_device, self.read_table, self.write_point = save_device, read_table, write_point
        self.can_write = can_write
        self.poll_table, self.stop_all = poll_table, stop_all
        self.subscribe_table = subscribe_table
        self._embedded = embedded
        self.state = SessionState()
        self._context = None
        self._tables = ()
        self.samples = SampleStore()
        self.sample_device_id = None
        self._definitions = None
        self._dirty = set()
        self._pending = set()
        layout = QVBoxLayout(self)
        self.context = QLabel()
        self.context.setTextFormat(Qt.TextFormat.PlainText)
        self.context.setWordWrap(True)
        layout.addWidget(self.context)
        self.tables = QComboBox()
        self.tables.setAccessibleName("点表")
        self.tables.setMinimumContentsLength(14)
        self.tables.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.tables, 1)
        self.actions = {}
        for label, slot in (("新建点表", self._new), ("重命名", self._rename), ("复制", self._duplicate),
                            ("删除点表", self._delete), ("导入…", self._import), ("导出 JSON…", self._export)):
            self._button(toolbar, label, slot)
        layout.addLayout(toolbar)
        search_bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setAccessibleName("搜索点位")
        self.search.setPlaceholderText("搜索名称、地址或功能描述（空格分隔多个关键词）")
        self.search.setClearButtonEnabled(True)
        search_bar.addWidget(self.search, 2)
        self.search_results = QComboBox()
        self.search_results.setAccessibleName("定位搜索结果")
        self.search_results.setMinimumContentsLength(18)
        self.search_results.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search_results.setToolTip("在当前设备的所有点表中搜索；选择结果跳转到对应点表和点位")
        search_bar.addWidget(self.search_results, 1)
        self.search_count = QLabel()
        search_bar.addWidget(self.search_count)
        layout.addLayout(search_bar)
        polling = QHBoxLayout()
        self.poll_interval = spinbox(200, 30000, 1000)
        self.poll_interval.setSuffix(" ms")
        self.stale_after = spinbox(200, 3600000, 3000)
        self.stale_after.setSuffix(" ms")
        for label, field in (("本表周期", self.poll_interval), ("过期时间", self.stale_after)):
            text = QLabel(label)
            text.setBuddy(field)
            polling.addWidget(text)
            polling.addWidget(field)
        self.poll_button = QPushButton("启动本表轮询")
        self.poll_button.clicked.connect(self._toggle_poll)
        polling.addWidget(self.poll_button)
        self.stop_all_button = QPushButton("停止全部采集")
        self.stop_all_button.clicked.connect(self.stop_all)
        polling.addWidget(self.stop_all_button)
        self.subscribe_button = QPushButton("订阅本表")
        self.subscribe_button.clicked.connect(self._subscribe)
        self.queue_size = spinbox(1, 1000, 10)
        self.queue_size.setPrefix("队列 ")
        self.queue_size.setToolTip("每个 MonitoredItem 的队列长度；丢弃最旧通知，显示修订参数与溢出标志")
        polling.addWidget(self.subscribe_button)
        polling.addWidget(self.queue_size)
        polling.addStretch()
        self.schedule_status = QLabel()
        layout.addLayout(polling)
        if embedded:
            layout.addWidget(self.schedule_status)
        else:
            polling.addWidget(self.schedule_status)
        toolbar = QHBoxLayout()
        for label, slot in (("新增点位", self._add_point), ("编辑点位", self._edit_point),
                            ("删除点位", self._delete_point), ("批量编辑…", self._bulk_edit)):
            self._button(toolbar, label, slot)
        toolbar.addStretch()
        self._button(toolbar, "读取整表", self._read).setObjectName("primary")
        self._button(toolbar, "写入点位…", self._write).setObjectName("write")
        layout.addLayout(toolbar)
        self.rows = Rows(("名称", "地址 / 类型", "原始值", "工程值", "单位", "最后采样", "质量", "最后成功", "功能描述", "二进制", "协议状态 / 时间戳"), self._row, (2, 3), (9,))
        self.rows.setParent(self)
        self.point_table = _table(self.rows)
        configure_point_reordering(self.point_table, self._move_point)
        self.point_table.setAccessibleName("命名点位")
        self.point_table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        self.point_table.setColumnWidth(8, 240)
        self.point_table.setMinimumHeight(150)
        self.point_description = QPlainTextEdit()
        self.point_description.setReadOnly(True)
        self.point_description.setAccessibleName("所选点位功能描述")
        self.point_description.setPlaceholderText("选择点位，查看用途、取值含义和操作注意事项。双击点位可编辑。")
        self.point_description.setMinimumWidth(180)
        self.point_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.point_splitter.setChildrenCollapsible(False)
        self.point_splitter.addWidget(self.point_table)
        self.point_splitter.addWidget(self.point_description)
        self.point_splitter.setSizes([760, 300])
        layout.addWidget(self.point_splitter, 1)
        self.empty = QLabel()
        self.empty.setObjectName("resultContext")
        layout.addWidget(self.empty)
        self.feedback = QLabel()
        self.feedback.setObjectName("warning")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        footer = QHBoxLayout()
        hint = QLabel("拖动左侧行号调整点位顺序。工程值 = 原始值 × 倍率 + 偏置；写入使用原始值。")
        hint.setWordWrap(True)
        footer.addWidget(hint, 1)
        self.bit_button = QPushButton("位查看…")
        self.bit_button.clicked.connect(lambda: show_bit_inspector(self, self._bit_reading))
        footer.addWidget(self.bit_button)
        self.save_device_button = QPushButton("保存设备配置")
        self.save_device_button.clicked.connect(self.save_device)
        footer.addWidget(self.save_device_button)
        layout.addLayout(footer)
        self.tables.currentIndexChanged.connect(self._show_points)
        self.search.textChanged.connect(self._search_changed)
        self.search_results.activated.connect(self._locate_point)
        self.point_table.selectionModel().currentRowChanged.connect(lambda *_: self._buttons())
        self.point_table.doubleClicked.connect(lambda *_: self._edit_point())
        self.poll_interval.valueChanged.connect(self._settings_changed)
        self.stale_after.valueChanged.connect(self._settings_changed)
        self._paint_timer = QTimer(self)
        self._paint_timer.setInterval(50)
        self._paint_timer.timeout.connect(self._flush_values)
        self._paint_timer.start()

    def _button(self, layout, name, slot):
        button = QPushButton(name)
        button.clicked.connect(slot)
        layout.addWidget(button)
        self.actions[name] = button
        return button

    @property
    def table(self):
        table_id = self.tables.currentData()
        return next((table for table in self._tables if table.id == table_id), None)

    @property
    def point(self):
        index = self.point_table.currentIndex()
        return self.rows.items[index.row()] if index.isValid() else None

    def _row(self, point):
        table = self.table
        sample = self.samples.samples.get((table.id, point.id)) if table else None
        raw, engineering, time, success, outcome = "—", "—", "—", "—", Quality.NEVER.value
        if sample:
            value, outcome = sample.raw_value, sample.quality.value
            time = sample.sampled_at.strftime("%H:%M:%S") if sample.sampled_at else "—"
            success = sample.last_success_at.strftime("%H:%M:%S") if sample.last_success_at else "—"
            if value is not None:
                raw = str(value)
                try:
                    engineering = point.engineering_text(value)
                except ValueError as error:
                    outcome = str(error)
                if sample.quality != Quality.GOOD:
                    outcome += " · 旧数据"
        if table and (table.id, point.id) in self._pending:
            outcome = "等待读取"
        bits = binary_text(sample.raw_value, target_kind(point.target)) if sample else "—"
        native = sample.detail if sample and sample.detail else sample.metadata.summary if sample and sample.metadata else "—"
        if sample and sample.changed_at:
            native += f" · 最后值变化 {sample.changed_at:%H:%M:%S}"
        return point.name, point.target.label, raw, engineering, point.unit, time, outcome, success, point.description, bits, native

    def _bit_reading(self):
        point, table = self.point, self.table
        if point is None or table is None:
            return None
        sample = self.samples.samples.get((table.id, point.id))
        pattern = bit_pattern(sample.raw_value, target_kind(point.target)) if sample else None
        context = f"{point.target.label}；质量：{sample.quality.value if sample else Quality.NEVER.value}"
        if sample and sample.raw_value is not None:
            context += f"；原始值：{sample.raw_value}"
            if sample.last_success_at:
                context += f"；最后成功：{sample.last_success_at:%H:%M:%S}"
        return BitReading(point.name, pattern, context, point.bit_definitions, target_kind(point.target))

    def refresh(self, state, closing=False):
        self.state, self.closing = state, closing
        definitions = self.workspace.project.point_tables
        if definitions is not self._definitions:
            scoped = definitions if self.sample_device_id is None else tuple(
                table for table in definitions if table.device_id == self.sample_device_id)
            self._dirty.update(self.samples.sync(scoped))
            self._definitions = definitions
        self._dirty.update(self.samples.set_generation(state.generation))
        if state.connection != ConnectionState.ONLINE:
            self._dirty.update(self.samples.disconnect())
        device = self.device()
        self._context = device
        tables = tuple(table for table in self.workspace.project.point_tables if device and table.device_id == device.id)
        if tables != self._tables:
            selected = self.tables.currentData()
            self._tables = tables
            with QSignalBlocker(self.tables):
                self.tables.clear()
                for table in tables:
                    self.tables.addItem(table.name, table.id)
                self.tables.setCurrentIndex(max(0, self.tables.findData(selected)))
            self._show_points()
            self._search_changed()
        if device:
            text = device.profile.endpoint
            if not self._embedded and device.name != text:
                text = f"{device.name} · {text}"
        else:
            text = "先填写设备连接参数，再保存设备配置并新建点表。"
        self.context.setText(text)
        self.save_device_button.setVisible(device is None)
        pending = {key for key in self._pending if key[0] in state.manual_tables}
        self._dirty.update(self._pending - pending)
        self._pending = pending
        groups = len(state.polling_groups) + int(state.polling)
        full = " · 队列已满" if state.queued >= state.queue_capacity else ""
        self.schedule_status.setText(f"轮询 {groups} 组 · 订阅 {len(state.subscription_groups)} 表 · 手动任务 {state.queued}/{state.queue_capacity}{full}")
        health = self.samples.subscription_health.get(self.table.id) if self.table else None
        if health and self.table.id in state.subscription_groups:
            last = health.last_notification_at.strftime("%H:%M:%S") if health.last_notification_at else "尚无值通知"
            self.schedule_status.setText(self.schedule_status.text() + f" · {health.detail} · 最后值通知 {last}")
        if state.recovering_tables:
            self.schedule_status.setText(self.schedule_status.text() + f" · 待恢复 {len(state.recovering_tables)} 组")
        self._buttons()

    def _buttons(self):
        table, point = self.table, self.point
        description = f"{point.name} · {point.target.label}\n{point.description or '尚未定义功能描述，双击点位可补充。'}" if point else ""
        if self.point_description.toPlainText() != description:
            self.point_description.setPlainText(description)
        available = self._context is not None and not getattr(self, "closing", False)
        subscribed = table is not None and table.id in self.state.subscription_groups
        active = table is not None and (table.id in self.state.polling_groups or subscribed or table.id in self.state.recovering_tables)
        editable = available and not active and (table is None or table.id not in self.state.manual_tables)
        set_point_reordering_enabled(self.point_table, editable and table is not None and not self.search.text().strip(),
                                     "清空搜索并停止本表采集后，可拖动左侧行号排序。")
        for name, button in self.actions.items():
            button.setEnabled(editable and (name in ("新建点表", "导入…") or table is not None))
        for name in ("编辑点位", "删除点位"):
            self.actions[name].setEnabled(editable and point is not None)
        idle = available and self.state.connection == ConnectionState.ONLINE and self.state.queued < self.state.queue_capacity
        self.actions["读取整表"].setEnabled(idle and table is not None and bool(table.points))
        writable = point is not None and getattr(point.target, "writable", True)
        if point is not None and hasattr(point.target, "area") and target_record(point.target)["area"] in ("IR", "DI"):
            writable = False
        self.actions["写入点位…"].setEnabled(idle and writable and self.can_write())
        for name in ("读取整表", "写入点位…"):
            self.actions[name].setToolTip("手动任务队列已满，请等待任务完成" if self.state.queued >= self.state.queue_capacity else "")
        self.tables.setEnabled(not getattr(self, "closing", False))
        self.save_device_button.setEnabled(not getattr(self, "closing", False))
        self.poll_interval.setEnabled(editable and table is not None)
        self.stale_after.setEnabled(editable and table is not None)
        self.poll_button.setText("停止本表轮询" if active else "启动本表轮询")
        self.poll_button.setEnabled(available and self.state.connection == ConnectionState.ONLINE and table is not None
                                    and bool(table.points) and table.id not in self.state.manual_tables)
        self.stop_all_button.setEnabled(not getattr(self, "closing", False) and
                                        (self.state.polling or bool(self.state.polling_groups) or bool(self.state.subscription_groups)
                                         or bool(self.state.recovering_tables)))
        opcua = self._context is not None and self._context.profile.protocol == "opcua"
        self.subscribe_button.setVisible(opcua)
        self.queue_size.setVisible(opcua)
        self.subscribe_button.setText("停止本表订阅" if subscribed else "订阅本表")
        self.subscribe_button.setEnabled(idle and table is not None and bool(table.points))
        self.queue_size.setEnabled(not subscribed)
        if subscribed:
            self.poll_button.setText("切换为轮询")

    def _move_point(self, source, destination):
        if not self.point_table.verticalHeader().sectionsMovable() or self.table is None:
            return
        points = reordered_points(self.table.points, self.rows.items, source, destination)
        if points == self.table.points:
            return
        selected = self.rows.items[source]
        self._apply(replace(self.table, points=points))
        self.point_table.selectRow(self.rows.items.index(selected))

    def _show_points(self, *_args, keep_selection=False):
        selected = self.point.id if keep_selection and self.point else None
        points = self.table.points if self.table else ()
        self.rows.reset(points)
        self.empty.setText("本表暂无点位，可点击「新增点位」。" if self.table else "暂无点表，可新建或导入。")
        self.empty.setVisible(not points)
        if self.table:
            with QSignalBlocker(self.poll_interval), QSignalBlocker(self.stale_after):
                self.poll_interval.setValue(self.table.interval_ms)
                self.stale_after.setValue(self.table.stale_after_ms)
        if selected:
            index = next((index for index, point in enumerate(points) if point.id == selected), None)
            if index is not None:
                self.point_table.selectRow(index)
        self._buttons()
        self._filter_points()
        self.workspace_changed.emit()

    def _search_changed(self, *_args):
        query = self.search.text().strip()
        with QSignalBlocker(self.search_results):
            self.search_results.clear()
            if query:
                for table in self._tables:
                    for point in table.points:
                        if _matches(point, query):
                            self.search_results.addItem(f"{table.name} / {point.name} · {point.target.label}", (table.id, point.id))
            self.search_results.setPlaceholderText(f"定位设备内结果（{self.search_results.count()}）")
            self.search_results.setCurrentIndex(-1)
        self.search_results.setVisible(bool(query))
        self._filter_points()

    def _filter_points(self):
        query = self.search.text().strip()
        visible = 0
        selected = self.point
        for row, point in enumerate(self.rows.items):
            match = _matches(point, query)
            self.point_table.setRowHidden(row, not match)
            visible += int(match)
        if selected and not _matches(selected, query):
            self.point_table.clearSelection()
            self.point_table.setCurrentIndex(self.rows.index(-1, -1))
        self.search_count.setText(f"本表 {visible}/{len(self.rows.items)}")
        self._buttons()

    def _locate_point(self, index):
        result = self.search_results.itemData(index)
        if result is None:
            return
        table_id, point_id = result
        self.tables.setCurrentIndex(self.tables.findData(table_id))
        row = next((row for row, point in enumerate(self.rows.items) if point.id == point_id), None)
        if row is not None:
            self.point_table.selectRow(row)
            self.point_table.scrollTo(self.rows.index(row, 0))
            self.point_table.setFocus()

    def _apply(self, table):
        if self.subscribe_table is not None and self._context is not None and self._context.profile.protocol == "opcua":
            self.subscribe_table(table, False, self.queue_size.value())
        self.workspace.project = self.workspace.project.with_table(table)
        self.feedback.clear()
        self.refresh(self.state)
        self.tables.setCurrentIndex(self.tables.findData(table.id))

    def _name(self, title, current=""):
        value, accepted = QInputDialog.getText(self, title, "点表名称", text=current)
        return value.strip() if accepted and value.strip() else None

    def _new(self):
        if self._context is not None:
            name = self._name("新建点表")
            if name:
                self._apply(PointTable(new_id(), self._context.id, name))

    def _rename(self):
        if self.table:
            name = self._name("重命名点表", self.table.name)
            if name:
                self._apply(replace(self.table, name=name))

    def _duplicate(self):
        if self.table:
            name = self._name("复制点表", self.table.name + " 副本")
            if name:
                self._apply(self.table.duplicate(name))

    def _delete(self):
        if self.table and QMessageBox.question(self, "删除点表", f"删除「{self.table.name}」及其中全部点位？",
                                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                               QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            table_id = self.table.id
            if self.subscribe_table is not None and self._context.profile.protocol == "opcua":
                self.subscribe_table(self.table, False, self.queue_size.value())
            self.workspace.project = self.workspace.project.without_table(table_id)
            self.refresh(self.state)

    def _add_point(self):
        self._edit_definition(None)

    def _edit_point(self):
        if self.point and self.actions["编辑点位"].isEnabled():
            self._edit_definition(self.point)

    def _edit_definition(self, original):
        if self.table is None or self._context is None:
            return
        dialog = PointDialog(self, self._context, self.create_form(), original)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                points = tuple(dialog.definition if point == original else point for point in self.table.points)
                if original is None:
                    points += (dialog.definition,)
                self._apply(replace(self.table, points=points))
        finally:
            dialog.deleteLater()

    def _delete_point(self):
        if self.point and QMessageBox.question(self, "删除点位", f"删除「{self.point.name}」？",
                                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                               QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._apply(replace(self.table, points=tuple(point for point in self.table.points if point.id != self.point.id)))

    def _bulk_edit(self):
        if self.table is None:
            return
        dialog = CsvEditDialog(self, self.table, self._context.profile.protocol, self.workspace.project.with_table)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._apply(replace(self.table, points=dialog.points))
        finally:
            dialog.deleteLater()

    def _import(self):
        if self._context is None:
            return
        selected, _ = QFileDialog.getOpenFileName(self, "导入点表", "", "点表配置 (*.json *.csv)")
        if not selected:
            return
        try:
            path = Path(selected)
            with path.open("rb") as stream:
                contents = stream.read(IMPORT_LIMIT_BYTES + 1)
            if len(contents) > IMPORT_LIMIT_BYTES:
                raise ValueError("点表文件超过 10 MiB")
            text = contents.decode("utf-8-sig")
            protocol = self._context.profile.protocol
            if path.suffix.lower() == ".csv":
                table = PointTable(new_id(), self._context.id, path.stem,
                                   import_points_csv(text, protocol, fresh_ids=True))
            else:
                table = import_table_json(text, protocol, self._context.id)
            self.workspace.project.with_table(table)
        except (OSError, ValueError, RecursionError) as error:
            self.feedback.setText(f"导入失败 · {error}")
            return
        preview = ImportPreview(self, table)
        try:
            if preview.exec() == QDialog.DialogCode.Accepted:
                self._apply(table)
        finally:
            preview.deleteLater()

    def _export(self):
        if self.table is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出点表", self.table.name + ".json", "JSON (*.json)")
        if path:
            try:
                contents = export_table_json(self.table, self._context.profile.protocol).encode("utf-8")
                _replace_file(Path(path), contents)
            except (OSError, ValueError) as error:
                self.feedback.setText(f"导出失败 · {error}")

    def _read(self):
        if self.table and self.actions["读取整表"].isEnabled():
            self._pending = {(self.table.id, point.id) for point in self.table.points}
            self.read_table(self.table)

    def _write(self):
        if self.point and self.actions["写入点位…"].isEnabled():
            self.write_point(self.point)

    def handle_read(self, event: TableReadUpdate):
        self._dirty.update(self.samples.update(event))
        self._pending.discard((event.table_id, event.point_id))

    def _settings_changed(self):
        if self.table and self.poll_interval.isEnabled():
            self._apply(replace(self.table, interval_ms=self.poll_interval.value(), stale_after_ms=self.stale_after.value()))

    def _toggle_poll(self):
        if self.table:
            self.poll_table(self.table, self.table.id not in self.state.polling_groups)

    def _subscribe(self):
        if self.table is not None and self.subscribe_table is not None:
            self.subscribe_table(self.table, self.table.id not in self.state.subscription_groups, self.queue_size.value())

    def _flush_values(self):
        self._dirty.update(self.samples.expire())
        if self.table:
            indices = [index for index, point in enumerate(self.rows.items) if (self.table.id, point.id) in self._dirty]
            # Emit contiguous ranges without resetting the model or selection.
            while indices:
                start = end = indices.pop(0)
                while indices and indices[0] == end + 1:
                    end = indices.pop(0)
                self.rows.dataChanged.emit(self.rows.index(start, 0), self.rows.index(end, len(self.rows.headers) - 1))
        self._dirty.clear()
