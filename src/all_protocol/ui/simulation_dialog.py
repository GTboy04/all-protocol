from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ..application.server_contracts import ListenerState
from ..application.simulation import (
    MAX_TIME_MS, MIN_PERIOD_MS, Assignment, RuleKind, SimulationCommand, SimulationRule, SimulationState, compile_rule,
)
from .widgets import EditDialog, spinbox


RULE_LABELS = {
    RuleKind.FIXED: "固定值", RuleKind.INCREMENT: "周期递增", RuleKind.HEARTBEAT: "心跳翻转",
    RuleKind.RAMP: "斜坡", RuleKind.SINE: "正弦", RuleKind.TIMELINE: "时间轴",
}


def _text(value):
    return "" if value is None else str(int(value) if isinstance(value, bool) else value)


def _plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class SimulationDialog(QDialog):
    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane
        self.setWindowTitle("服务端场景模拟")
        self.resize(520, 270)
        self._points = list(pane.configuration_plan().simulation_points())
        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label("停止服务后可配置规则。启动服务后，手动启动场景。"))
        controls = QHBoxLayout()
        self.start = self._button(controls, "启动场景", lambda: self._send(SimulationCommand.START))
        self.pause = self._button(controls, "暂停场景", lambda: self._send(SimulationCommand.PAUSE))
        self.reset = self._button(controls, "重置场景", lambda: self._send(SimulationCommand.RESET))
        self.reset.setToolTip("停止场景并恢复规则点位的方案初始值，同时清除接管状态")
        controls.addStretch()
        layout.addLayout(controls)

        form = QFormLayout()
        self.status = _plain_label()
        form.addRow("场景状态", self.status)
        self.point = QComboBox()
        self.point.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.point.setMinimumContentsLength(18)
        for point in self._points:
            self.point.addItem(point.label)
        self.point.setPlaceholderText("请先在方案中添加点位")
        form.addRow("点位", self.point)
        self.rule = _plain_label()
        form.addRow("模拟规则", self.rule)
        self.point_status = _plain_label()
        form.addRow("点位状态", self.point_status)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.edit = self._button(actions, "配置规则…", self._edit)
        self.remove = self._button(actions, "移除规则", lambda: self._set_rule(None))
        self.release = self._button(actions, "恢复自动模拟", lambda: self._send(SimulationCommand.RELEASE))
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.point.currentIndexChanged.connect(self.refresh)
        pane.simulation_changed.connect(self.refresh)
        pane.listener_changed.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _button(layout, label, slot):
        button = QPushButton(label)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

    def _selected(self):
        row = self.point.currentIndex()
        return self._points[row] if row >= 0 else None

    def refresh(self):
        update = self.pane.simulation_update
        running = self.pane.state == ListenerState.LISTENING and update is not None
        stopped = self.pane.state == ListenerState.STOPPED
        active = running and update.state == SimulationState.RUNNING
        point = self._selected()
        rule = point.rule if point else None
        held = running and point is not None and point.target in update.held
        fault = dict(update.faults).get(point.target) if running and point else None
        self.status.setText(self.pane.scene_status.text())
        self.rule.setText(RULE_LABELS[rule.kind] if rule else "未配置")
        self.point.setToolTip(point.label if point else "")
        self.point_status.setText(fault or ("已接管" if held else "自动模拟" if active and rule else "—"))
        self.start.setText("继续场景" if running and update.state == SimulationState.PAUSED else "启动场景")
        self.start.setEnabled(running and not active)
        self.pause.setEnabled(active)
        self.reset.setEnabled(running)
        self.edit.setEnabled(stopped and point is not None)
        self.remove.setEnabled(stopped and rule is not None)
        self.release.setEnabled(held and not fault)

    def _send(self, command):
        point = self._selected()
        self.pane.bridge.submit(self.pane.bridge.session.control_simulation(command, point.target if point else None))

    def _set_rule(self, rule):
        row = self.point.currentIndex()
        point = self._points[row]
        self.pane.set_simulation_rule(point.target, rule)
        self._points[row] = replace(point, rule=rule)
        self.refresh()

    def _edit(self):
        dialog = RuleDialog(self, self._selected(), self._set_rule)
        dialog.exec()
        dialog.deleteLater()


class RuleDialog(EditDialog):
    def __init__(self, parent, point, save):
        super().__init__(parent, "配置模拟规则", self._commit)
        self._point, self._save = point, save
        rule = point.rule or SimulationRule(RuleKind.FIXED)
        self.kind = QComboBox()
        kinds = (RuleKind.FIXED, RuleKind.HEARTBEAT, RuleKind.TIMELINE) if point.boolean else (
            RuleKind.FIXED, RuleKind.INCREMENT, RuleKind.RAMP, RuleKind.SINE, RuleKind.TIMELINE,
        )
        for kind in kinds:
            self.kind.addItem(RULE_LABELS[kind], kind.value)
        self.kind.setCurrentIndex(self.kind.findData(RuleKind(rule.kind).value))
        self.start = spinbox(0, MAX_TIME_MS, rule.start_ms)
        self.period = spinbox(MIN_PERIOD_MS, MAX_TIME_MS, rule.period_ms)
        self.value = QLineEdit(_text(rule.value))
        self.value.setPlaceholderText(f"默认使用方案初始值 {_text(point.initial)}")
        self.step = QLineEdit(_text(rule.step))
        self.low = QLineEdit(_text(rule.low))
        self.high = QLineEdit(_text(rule.high))
        self.amplitude = QLineEdit(_text(rule.amplitude))
        self.actions = QPlainTextEdit("\n".join(f"{item.at_ms} = {_text(item.value)}" for item in rule.actions))
        self.actions.setPlaceholderText("每行一个动作，时间严格递增（毫秒）\n例如：\n0 = 0\n500 = 1")
        self.actions.setFixedHeight(100)
        self.form.addRow("点位", _plain_label(point.label))
        for caption, field in (
            ("规则", self.kind), ("延时（ms）", self.start), ("周期（ms）", self.period),
            ("起始值", self.value), ("步长", self.step), ("下限", self.low), ("上限", self.high),
            ("幅度", self.amplitude), ("赋值动作", self.actions),
        ):
            self.form.addRow(caption, field)
        self.kind.currentIndexChanged.connect(self._refresh)
        self._refresh()
        self.resize(440, self.sizeHint().height())

    def _kind(self):
        return RuleKind(self.kind.currentData())

    def _refresh(self):
        kind = self._kind()
        timeline = kind == RuleKind.TIMELINE
        increment = kind == RuleKind.INCREMENT
        for field, visible in (
            (self.start, not timeline), (self.period, kind not in (RuleKind.FIXED, RuleKind.TIMELINE)),
            (self.value, not timeline), (self.step, increment), (self.low, increment),
            (self.high, increment or kind == RuleKind.RAMP), (self.amplitude, kind == RuleKind.SINE),
            (self.actions, timeline),
        ):
            self.form.setRowVisible(field, visible)
        self.form.labelForField(self.high).setText("终值" if kind == RuleKind.RAMP else "上限")
        self.form.labelForField(self.value).setText("中心值" if kind == RuleKind.SINE else "起始值")
        duration = {RuleKind.HEARTBEAT: "翻转间隔", RuleKind.RAMP: "斜坡时长"}.get(kind, "周期")
        self.form.labelForField(self.period).setText(f"{duration}（ms）")

    def _commit(self):
        kind = self._kind()
        number = float if self._point.floating else int
        try:
            if kind == RuleKind.TIMELINE:
                lines = (line.split("=", 1) for line in self.actions.toPlainText().splitlines() if line.strip())
                rule = SimulationRule(kind, actions=tuple(Assignment(int(time), number(value)) for time, value in lines))
            else:
                value = number(self.value.text()) if self.value.text().strip() else None
                rule = SimulationRule(
                    kind, self.start.value(), self.period.value() if kind != RuleKind.FIXED else 1000, value,
                    step=number(self.step.text()) if kind == RuleKind.INCREMENT else 1,
                    low=number(self.low.text()) if kind == RuleKind.INCREMENT else None,
                    high=number(self.high.text()) if kind in (RuleKind.INCREMENT, RuleKind.RAMP) else None,
                    amplitude=number(self.amplitude.text()) if kind == RuleKind.SINE else None,
                )
        except ValueError as error:
            raise ValueError("每行使用“毫秒 = 数值”" if kind == RuleKind.TIMELINE else "请输入有效的十进制数值") from error
        compile_rule(self._point, rule)
        self._save(rule)
