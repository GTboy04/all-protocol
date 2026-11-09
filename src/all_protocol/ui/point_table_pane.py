from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..application.contracts import ClientError, ConnectionState, SessionState, TableReadUpdate
from ..application.sampling import Quality, SampleStore
from ..point_tables import (
    IMPORT_LIMIT_BYTES, PointDefinition, PointTable, export_points_csv, export_table_json,
    import_points_csv, import_table_json, new_id, target_record,
)
from ..storage import _replace_file
from .main_window import Rows, _table
from .server_pane import EditDialog
from .widgets import spinbox


def _definition_row(point):
    return (point.name, point.target.label, point.unit, str(point.scale), str(point.offset), point.description)


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
        self.description = QLineEdit(original.description if original else "")
        for label, field in (("名称", self.name), ("协议地址", self.target_form.query), ("单位", self.unit),
                             ("倍率", self.scale), ("偏置", self.offset), ("小数位数", self.precision),
                             ("说明", self.description)):
            self.form.addRow(label, field)
        self.protocol = device.profile.protocol
        self.resize(760, 420)

    def _commit(self):
        try:
            definition = PointDefinition(self.original.id if self.original else new_id(), self.name.text().strip(),
                                         self.target_form.target(), self.unit.text(), float(self.scale.text()),
                                         float(self.offset.text()), self.description.text(), self.precision.value())
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
                 poll_table, stop_all, *, embedded=False):
        super().__init__()
        self.workspace, self.device, self.create_form = workspace, device, create_form
        self.save_device, self.read_table, self.write_point = save_device, read_table, write_point
        self.can_write = can_write
        self.poll_table, self.stop_all = poll_table, stop_all
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
        from PySide6.QtWidgets import QComboBox
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
        self.stop_all_button = QPushButton("停止全部轮询")
        self.stop_all_button.clicked.connect(self.stop_all)
        polling.addWidget(self.stop_all_button)
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
        self._button(toolbar, "写入原始值…", self._write).setObjectName("write")
        layout.addLayout(toolbar)
        self.rows = Rows(("名称", "地址 / 类型", "原始值", "工程值", "单位", "最后采样", "质量", "最后成功", "说明"), self._row, (2, 3))
        self.rows.setParent(self)
        self.point_table = _table(self.rows)
        self.point_table.setAccessibleName("命名点位")
        layout.addWidget(self.point_table, 1)
        self.empty = QLabel()
        self.empty.setObjectName("resultContext")
        layout.addWidget(self.empty)
        self.feedback = QLabel()
        self.feedback.setObjectName("warning")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        footer = QHBoxLayout()
        hint = QLabel("工程值 = 原始值 × 倍率 + 偏置；可按表独立轮询，写入使用原始值。")
        hint.setWordWrap(True)
        footer.addWidget(hint, 1)
        self.save_device_button = QPushButton("保存设备配置")
        self.save_device_button.clicked.connect(self.save_device)
        footer.addWidget(self.save_device_button)
        layout.addLayout(footer)
        self.tables.currentIndexChanged.connect(self._show_points)
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
        return point.name, point.target.label, raw, engineering, point.unit, time, outcome, success, point.description

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
        self.schedule_status.setText(f"轮询 {groups} 组 · 手动任务 {state.queued}/{state.queue_capacity}{full}")
        self._buttons()

    def _buttons(self):
        table, point = self.table, self.point
        available = self._context is not None and not getattr(self, "closing", False)
        active = table is not None and table.id in self.state.polling_groups
        editable = available and not active and (table is None or table.id not in self.state.manual_tables)
        for name, button in self.actions.items():
            button.setEnabled(editable and (name in ("新建点表", "导入…") or table is not None))
        for name in ("编辑点位", "删除点位"):
            self.actions[name].setEnabled(editable and point is not None)
        idle = available and self.state.connection == ConnectionState.ONLINE and self.state.queued < self.state.queue_capacity
        self.actions["读取整表"].setEnabled(idle and table is not None and bool(table.points))
        writable = point is not None and getattr(point.target, "writable", True)
        if point is not None and hasattr(point.target, "area") and target_record(point.target)["area"] in ("IR", "DI"):
            writable = False
        self.actions["写入原始值…"].setEnabled(idle and writable and self.can_write())
        for name in ("读取整表", "写入原始值…"):
            self.actions[name].setToolTip("手动任务队列已满，请等待任务完成" if self.state.queued >= self.state.queue_capacity else "")
        self.tables.setEnabled(not getattr(self, "closing", False))
        self.save_device_button.setEnabled(not getattr(self, "closing", False))
        self.poll_interval.setEnabled(editable and table is not None)
        self.stale_after.setEnabled(editable and table is not None)
        self.poll_button.setText("停止本表轮询" if active else "启动本表轮询")
        self.poll_button.setEnabled(available and self.state.connection == ConnectionState.ONLINE and table is not None
                                    and bool(table.points) and table.id not in self.state.manual_tables)
        self.stop_all_button.setEnabled(self.state.connection == ConnectionState.ONLINE
                                        and (self.state.polling or bool(self.state.polling_groups)))

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
        self.workspace_changed.emit()

    def _apply(self, table):
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
        if self.point and self.actions["写入原始值…"].isEnabled():
            self.write_point(self.point.target)

    def handle_read(self, event: TableReadUpdate):
        self._dirty.update(self.samples.update(event))
        self._pending.discard((event.table_id, event.point_id))

    def _settings_changed(self):
        if self.table and self.poll_interval.isEnabled():
            self._apply(replace(self.table, interval_ms=self.poll_interval.value(), stale_after_ms=self.stale_after.value()))

    def _toggle_poll(self):
        if self.table:
            self.poll_table(self.table, self.table.id not in self.state.polling_groups)

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
