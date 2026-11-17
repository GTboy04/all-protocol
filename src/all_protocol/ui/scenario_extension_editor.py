"""Station-scoped conditional forms and trusted Python definition editor."""
import json
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QTabWidget, QTableWidget,
    QVBoxLayout, QWidget, QScrollArea, QAbstractItemView, QStackedWidget,
)

from ..application.scenario_model import (
    ConditionalRule, PointRef, ScenarioExtension, ScenarioValue, SequenceStep, check_extension,
    decode_extension, extension_format_version, extension_record, validate_extensions, MAX_MANIFEST_BYTES,
)
from ..application.server_contracts import ListenerState
from ..scenario_trust import trust
from ..scenario_worker import compile_source
from .widgets import spinbox


EXAMPLE = '''from all_protocol.scenario_sdk import Scene, Step


def build(scene: Scene) -> None:
    command = scene.point("HR", 0)
    state = scene.point("HR", 10)
    scene.when_equal(
        id="startup_sequence", source=command, value=10,
        completion="hold",
    ).sequence([
        Step(at_ms=0, values={state: 1}),
        Step(at_ms=2000, values={state: 2}),
        Step(at_ms=5000, values={state: 3}),
        Step(at_ms=8000, values={state: 0}),
    ])
'''


def _label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class _CheckThread(QThread):
    result = Signal(object, str)

    def __init__(self, source, parent):
        super().__init__(parent)
        self.source = source
        self.cancelled = Event()

    def run(self):
        try:
            rules = compile_source(self.source, cancelled=self.cancelled)
        except Exception as error:
            self.result.emit(None, str(error))
        else:
            self.result.emit(rules, "")


class ScenarioEditor(QDialog):
    def __init__(self, parent, station, extension, save):
        super().__init__(parent)
        self.station, self.original, self.save = station, extension, save
        self.checked = None
        self.worker = None
        self._checking_source = ""
        self.setWindowTitle(f"场景扩展 · Unit {station.unit_id}")
        self.resize(900, 740)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"配置范围：仅 Unit {station.unit_id} · {station.name}。偏移为 0 基。"))
        form = QFormLayout()
        self.name = QLineEdit(extension.name if extension else "条件联动")
        self.enabled = QCheckBox("启用此扩展（场景启动后按规则执行）")
        self.enabled.setChecked(extension.enabled if extension else True)
        form.addRow("名称", self.name)
        form.addRow(self.enabled)
        layout.addLayout(form)
        self.tabs = QTabWidget()
        self.form_page = QWidget()
        basic = QVBoxLayout(self.form_page)
        fields = self.condition_fields = QFormLayout()
        self.source = self._point_picker()
        self.compare = QComboBox()
        for op, title in (("eq", "等于"), ("ne", "不等于"), ("gt", "大于"), ("ge", "大于等于"),
                          ("lt", "小于"), ("le", "小于等于"), ("always", "无条件")):
            self.compare.addItem(title, op)
        self.condition = QLineEdit("10")
        self.abs_tol, self.rel_tol = QLineEdit("0.000001"), QLineEdit("0.000001")
        self.completion = QComboBox()
        self.completion.addItem("保持最终值和输出控制权", "hold")
        self.completion.addItem("完成后释放，恢复基础场景", "release")
        self.initial = QCheckBox("首次启动已满足时也触发")
        self.cancel_false = QCheckBox("条件失效时取消后续步骤")
        comparison = QHBoxLayout()
        comparison.addWidget(self.compare)
        comparison.addWidget(self.condition)
        for caption, widget in (("源点位", self.source), ("条件 / 比较值", comparison),
                                ("浮点绝对容差", self.abs_tol), ("浮点相对容差", self.rel_tol),
                                ("完成策略", self.completion)):
            fields.addRow(caption, widget)
        options = QHBoxLayout()
        options.addWidget(self.initial)
        options.addWidget(self.cancel_false)
        fields.addRow(options)
        self.always_hint = _label("无条件：启动场景时执行一次；暂停后继续不重复。重置场景或重新启用扩展 / 从站后可再次执行。")
        fields.addRow(self.always_hint)
        basic.addLayout(fields)
        basic.addWidget(_label("动作时间为触发后的绝对毫秒；0 为立即，正延时至少 20 ms。取点位值：动作执行时读取所选点位的当前值。"))
        self.steps = QTableWidget(0, 4)
        self.steps.setHorizontalHeaderLabels(("触发后（ms）", "目标点位", "写入方式", "写入值 / 取值点位"))
        self.steps.horizontalHeader().setStretchLastSection(True)
        self.steps.setColumnWidth(0, 100)
        self.steps.setColumnWidth(1, 275)
        self.steps.setColumnWidth(2, 135)
        self.steps.verticalHeader().setDefaultSectionSize(42)
        self.steps.setMinimumHeight(180)
        self.steps.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        basic.addWidget(self.steps)
        rows = QHBoxLayout()
        add, remove = QPushButton("添加动作"), QPushButton("删除选中动作")
        add.clicked.connect(lambda: self.add_step())
        remove.clicked.connect(self._remove_step)
        rows.addWidget(add)
        rows.addWidget(remove)
        basic.addLayout(rows)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_page)
        self.tabs.addTab(scroll, "条件联动")
        python_page = QWidget()
        python_layout = QVBoxLayout(python_page)
        python_layout.addWidget(_label("Python 仅在检查时生成执行计划；运行由引擎调度。独立进程不是安全沙箱，只检查可信代码。"))
        self.trusted = QCheckBox("我信任此代码，允许在本机检查")
        python_layout.addWidget(self.trusted)
        self.code = QPlainTextEdit(extension.source if extension and extension.mode == "python" else EXAMPLE)
        self.code.setStyleSheet("font-family: Consolas; font-size: 12px;")
        python_layout.addWidget(self.code)
        self.tabs.addTab(python_page, "Python 扩展")
        layout.addWidget(self.tabs, 1)
        self.check = QPushButton("检查并预览")
        self.check.clicked.connect(self.check_definition)
        layout.addWidget(self.check)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(110)
        self.preview.setPlaceholderText("检查后显示实际 Unit ID、条件、动作时间和目标。应用后仍需保存方案或项目。")
        layout.addWidget(self.preview)
        self.error = _label("")
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.apply = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply.setText("应用扩展")
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self._apply)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if extension and extension.mode == "form" and len(extension.rules) == 1:
            rule = extension.rules[0]
            self.source.setCurrentIndex(next((i for i in range(self.source.count()) if self.source.itemData(i) == rule.source), -1))
            self.compare.setCurrentIndex(self.compare.findData(rule.op))
            self.condition.setText(str(rule.value))
            self.completion.setCurrentIndex(self.completion.findData(rule.completion))
            self.initial.setChecked(rule.fire_initial)
            self.cancel_false.setChecked(rule.cancel_on_false)
            self.abs_tol.setText(str(rule.abs_tol or 0))
            self.rel_tol.setText(str(rule.rel_tol or 0))
            for step in rule.steps:
                for value in step.values:
                    self.add_step(step.at_ms, value.target, value.value, value.value_source)
        else:
            self.add_step(0)
            if extension and extension.mode == "python":
                self.tabs.setCurrentIndex(1)
        self.name.textChanged.connect(self._invalidate)
        self.enabled.toggled.connect(self._invalidate)
        self.code.textChanged.connect(self._invalidate)
        self.tabs.currentChanged.connect(self._invalidate)
        for field in (self.condition, self.abs_tol, self.rel_tol):
            field.textChanged.connect(self._invalidate)
        for field in (self.source, self.compare, self.completion):
            field.currentIndexChanged.connect(self._invalidate)
        self.initial.toggled.connect(self._invalidate)
        self.cancel_false.toggled.connect(self._invalidate)
        self.source.currentIndexChanged.connect(self._refresh_source)
        self.compare.currentIndexChanged.connect(self._refresh_source)
        self._refresh_source()
        if self.steps.rowCount():
            self.steps.setCurrentCell(0, 0)
            self.steps.scrollToTop()

    def _point_picker(self):
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(18)
        for point in self.station.points:
            combo.addItem(f"Unit {self.station.unit_id} / {point.area} / {point.offset} · {point.name}",
                          PointRef(str(point.area), point.offset))
        return combo

    def _refresh_source(self, *_):
        unconditional = self.compare.currentData() == "always"
        self.source.setEnabled(not unconditional)
        self.condition.setEnabled(not unconditional)
        self.initial.setEnabled(not unconditional)
        self.cancel_false.setEnabled(not unconditional)
        self.condition_fields.setRowVisible(self.always_hint, unconditional)
        ref = self.source.currentData()
        point = next((p for p in self.station.points if ref and (str(p.area), p.offset) == (ref.area, ref.offset)), None)
        visible = bool(point and point.value_type.floating and self.compare.currentData() in ("eq", "ne"))
        self.condition_fields.setRowVisible(self.abs_tol, visible)
        self.condition_fields.setRowVisible(self.rel_tol, visible)

    def _invalidate(self, *_):
        self.checked = None
        self.apply.setEnabled(False)

    def add_step(self, at_ms=None, target=None, value=1, value_source=None):
        row = self.steps.rowCount()
        self.steps.insertRow(row)
        at = spinbox(0, 2_147_483_647, at_ms if at_ms is not None else (row * 1000))
        combo = self._point_picker()
        if target is not None:
            combo.setCurrentIndex(next((i for i in range(combo.count()) if combo.itemData(i) == target), -1))
        text = QLineEdit(str(value))
        mode = QComboBox()
        mode.addItem("手动填写", "literal")
        mode.addItem("取点位值", "point")
        picker = self._point_picker()
        ref = value_source or self.source.currentData()
        if ref is not None:
            picker.setCurrentIndex(next((i for i in range(picker.count()) if picker.itemData(i) == ref), -1))
        stack = QStackedWidget()
        stack.addWidget(text)
        stack.addWidget(picker)
        mode.setCurrentIndex(1 if value_source is not None else 0)
        stack.setCurrentIndex(mode.currentIndex())
        mode.currentIndexChanged.connect(stack.setCurrentIndex)
        for col, widget in enumerate((at, combo, mode, stack)):
            self.steps.setCellWidget(row, col, widget)
        self.steps.setCurrentCell(row, 0)
        at.valueChanged.connect(self._invalidate)
        combo.currentIndexChanged.connect(self._invalidate)
        text.textChanged.connect(self._invalidate)
        mode.currentIndexChanged.connect(self._invalidate)
        picker.currentIndexChanged.connect(self._invalidate)
        if hasattr(self, "apply"):
            self._invalidate()

    def _remove_step(self):
        self.steps.removeRow(self.steps.currentRow())
        self._invalidate()

    def _value(self, ref, text):
        if ref is None:
            raise ValueError("请选择当前从站的点位")
        point = next((p for p in self.station.points if (str(p.area), p.offset) == (ref.area, ref.offset)), None)
        if point is None:
            raise ValueError("请选择当前从站的点位")
        return point.value_type.parse(text)

    def _form_rules(self):
        unconditional = self.compare.currentData() == "always"
        source = None if unconditional else self.source.currentData()
        if source is None and not unconditional:
            raise ValueError("请先配置点位")
        floating = not unconditional and next(p.value_type.floating for p in self.station.points
                        if (str(p.area), p.offset) == (source.area, source.offset))
        steps = []
        for row in range(self.steps.rowCount()):
            time = self.steps.cellWidget(row, 0).value()
            target = self.steps.cellWidget(row, 1).currentData()
            stack = self.steps.cellWidget(row, 3)
            if self.steps.cellWidget(row, 2).currentData() == "point":
                value_source = stack.widget(1).currentData()
                if value_source is None:
                    raise ValueError("请选择取值点位")
                value = ScenarioValue(target, 0, value_source)
            else:
                value = ScenarioValue(target, self._value(target, stack.widget(0).text()))
            if steps and steps[-1].at_ms == time:
                steps[-1] = SequenceStep(time, steps[-1].values + (value,))
            else:
                steps.append(SequenceStep(time, (value,)))
        return (ConditionalRule(self.original.rules[0].id if self.original and self.original.rules else "sequence",
                                source, 0 if unconditional else self._value(source, self.condition.text()), tuple(steps),
                                op=self.compare.currentData(), completion=self.completion.currentData(),
                                abs_tol=float(self.abs_tol.text()) if floating else None,
                                rel_tol=float(self.rel_tol.text()) if floating else None,
                                fire_initial=not unconditional and self.initial.isChecked(),
                                cancel_on_false=not unconditional and self.cancel_false.isChecked()),)

    def check_definition(self):
        self._invalidate()
        self.error.clear()
        if self.worker is not None and self.worker.isRunning():
            return
        if self.tabs.currentIndex() == 1:
            if not self.trusted.isChecked():
                self.error.setText("请确认代码可信后勾选检查许可。")
                return
            self._checking_source = self.code.toPlainText()
            self.worker = _CheckThread(self._checking_source, self)
            self.worker.result.connect(self._checked_python)
            self.worker.finished.connect(lambda: self.check.setEnabled(True))
            self.check.setEnabled(False)
            self.worker.start()
        else:
            try:
                self._finish_check(self._form_rules(), "form", "")
            except (ValueError, TypeError) as error:
                self.error.setText(str(error))

    def _checked_python(self, rules, error):
        if error:
            self.error.setText(error)
        elif self._checking_source != self.code.toPlainText() or self.tabs.currentIndex() != 1:
            self.error.setText("代码已变化，请重新检查。")
        else:
            try:
                self._finish_check(rules, "python", self._checking_source)
            except (ValueError, TypeError) as failure:
                self.error.setText(str(failure))

    def _finish_check(self, rules, mode, source):
        extension = ScenarioExtension(self.original.id if self.original else str(uuid4()), self.name.text().strip(),
                                      tuple(rules), mode, source, self.enabled.isChecked())
        self.checked = check_extension(self.station, extension)
        others = tuple(e for e in self.station.extensions if e.id != extension.id)
        validate_extensions(replace(self.station, extensions=others + (self.checked,)))
        lines = [f"Unit {self.station.unit_id} · {extension.name} · {len(rules)} 条规则"]
        for rule in rules:
            completion = "完成后保持输出" if rule.completion == "hold" else "完成后释放输出"
            if rule.op == "always":
                condition = "无条件（启动场景时执行一次）"
            else:
                operator = {"eq": "等于", "ne": "不等于", "gt": "大于", "ge": "大于等于", "lt": "小于", "le": "小于等于"}[rule.op]
                condition = f"{rule.source.area}/{rule.source.offset} {operator} {rule.value}"
            lines.append(f"{rule.id}: {condition} → {completion}")
            for step in rule.steps:
                assignments = []
                for item in step.values:
                    value = (f"Unit {self.station.unit_id}/{item.value_source.area}/{item.value_source.offset} 的当前值"
                             if item.value_source else str(item.value))
                    assignments.append(f"Unit {self.station.unit_id}/{item.target.area}/{item.target.offset} = {value}")
                lines.append(f"  +{step.at_ms} ms: " + ", ".join(assignments))
        self.preview.setPlainText("\n".join(lines))
        self.error.setText("检查通过。既有基础场景配置将保留。")
        self.apply.setEnabled(True)

    def _apply(self):
        if self.checked is None:
            return
        try:
            if self.checked.mode == "python":
                trust(self.checked)
            self.save(self.checked)
        except (OSError, ValueError) as error:
            self.error.setText(str(error))
            return
        self.accept()

    def done(self, result):
        if self.worker is not None:
            self.worker.cancelled.set()
            self.worker.wait(4500)
        super().done(result)


class ScenarioExtensionsWidget(QWidget):
    def __init__(self, pane, unit, parent=None):
        super().__init__(parent)
        self.pane, self.unit = pane, unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label("条件联动 / Python 扩展（当前从站）"))
        self.items = QListWidget()
        self.items.setMaximumHeight(110)
        self.items.currentRowChanged.connect(self.refresh_controls)
        layout.addWidget(self.items)
        row = QHBoxLayout()
        self.add = QPushButton("新增扩展…")
        self.edit = QPushButton("编辑 / 检查…")
        self.remove = QPushButton("删除扩展")
        self.export = QPushButton("导出扩展…")
        self.import_ = QPushButton("导入扩展…")
        for button in (self.add, self.edit, self.remove, self.export, self.import_):
            row.addWidget(button)
        layout.addLayout(row)
        self.add.clicked.connect(lambda: self._edit(None))
        self.edit.clicked.connect(lambda: self._edit(self.selected()))
        self.remove.clicked.connect(self._remove)
        self.export.clicked.connect(self._export)
        self.import_.clicked.connect(self._import)
        runtime = QHBoxLayout()
        self.enable, self.disable, self.release = QPushButton("启用扩展"), QPushButton("停用扩展"), QPushButton("释放输出 / 恢复基础场景")
        for button, action in ((self.enable, "enable"), (self.disable, "disable"), (self.release, "release")):
            button.clicked.connect(lambda checked=False, action=action: self._control(action))
            runtime.addWidget(button)
        layout.addLayout(runtime)
        self.status = _label("")
        layout.addWidget(self.status)
        pane.listener_changed.connect(self.refresh_controls)
        pane.scenario_changed.connect(self.refresh_controls)
        self.set_unit(unit)

    def station(self):
        return next((s for s in self.pane.configuration_plan().stations if s.unit_id == self.unit), None)

    def selected(self):
        station = self.station()
        item = self.items.currentItem()
        return next((e for e in station.extensions if item and e.id == item.data(Qt.ItemDataRole.UserRole)), None) if station else None

    def set_unit(self, unit):
        self.unit = unit
        self.reload()

    def reload(self):
        from PySide6.QtWidgets import QListWidgetItem
        previous = self.selected()
        self.items.clear()
        station = self.station()
        if station:
            for extension in station.extensions:
                mode = "Python" if extension.mode == "python" else "条件联动"
                item = QListWidgetItem(f"{extension.name} · {mode} · {'启用' if extension.enabled else '停用'}")
                item.setData(Qt.ItemDataRole.UserRole, extension.id)
                self.items.addItem(item)
                if previous and extension.id == previous.id:
                    self.items.setCurrentItem(item)
        if self.items.currentRow() < 0 and self.items.count():
            self.items.setCurrentRow(0)
        self.refresh_controls()

    def refresh_controls(self):
        stopped = self.pane.state == ListenerState.STOPPED
        selected = self.selected()
        self.add.setEnabled(stopped and self.station() is not None)
        self.import_.setEnabled(stopped and self.station() is not None)
        for button in (self.edit, self.remove):
            button.setEnabled(stopped and selected is not None)
        self.export.setEnabled(selected is not None)
        for button in (self.enable, self.disable, self.release):
            button.setEnabled(self.pane.state == ListenerState.LISTENING and selected is not None)
        update = self.pane.scenario_update
        if update and selected:
            self.status.setText("；".join(f"{s.rule_id}: {s.state} · 步骤 {s.step}" +
                                         (f" · 剩余 {s.remaining_ms} ms" if s.remaining_ms is not None else "")
                                         for s in update.statuses if (s.unit_id, s.extension_id) == (self.unit, selected.id)))
        else:
            self.status.setText(f"配置范围：Unit {self.unit}" if self.unit is not None else "请先添加从站")

    def _edit(self, extension):
        station = self.station()
        if station is None:
            return
        def save(value):
            extensions = tuple(e for e in station.extensions if e.id != value.id) + (value,)
            self.pane.set_scenario_extensions(station.unit_id, extensions)
            self.reload()
        dialog = ScenarioEditor(self, station, extension, save)
        dialog.exec()
        dialog.deleteLater()

    def _remove(self):
        selected = self.selected()
        if selected:
            self.pane.set_scenario_extensions(self.unit, tuple(e for e in self.station().extensions if e.id != selected.id))
            self.reload()

    def _control(self, action):
        selected = self.selected()
        if selected:
            self.pane.bridge.submit(self.pane.bridge.session.control_extension(self.unit, selected.id, action))

    def _export(self):
        extension = self.selected()
        if extension is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出扩展", "scenario.json", "JSON (*.json)")
        if path:
            try:
                from ..storage import _replace_file
                _replace_file(Path(path), json.dumps({"format": "all-protocol-scenario", "version": extension_format_version(extension),
                                                      "extension": extension_record(extension)}, ensure_ascii=False, indent=2).encode())
            except (OSError, ValueError) as error:
                self.status.setText(str(error))

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入扩展到当前从站", "", "JSON (*.json)")
        if not path:
            return
        try:
            with Path(path).open("rb") as stream:
                contents = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(contents) > MAX_MANIFEST_BYTES:
                raise ValueError("扩展包超过 1 MiB")
            data = json.loads(contents.decode("utf-8-sig"))
            if (not isinstance(data, dict) or data.get("format") != "all-protocol-scenario"
                    or type(data.get("version")) is not int or data["version"] not in (1, 2)):
                raise ValueError("扩展包版本无效")
            extension = decode_extension(data["extension"])
            if data["version"] < extension_format_version(extension):
                raise ValueError("点位取值或无条件规则须使用扩展包 v2")
            extension = extension.copied()
            self.pane.set_scenario_extensions(self.unit, self.station().extensions + (extension,))
            self.reload()
            self.status.setText("已导入为停用状态；请编辑、检查并应用后保存方案。")
        except (KeyError, OSError, ValueError, TypeError) as error:
            self.status.setText(f"导入失败：{error}")
