from PySide6.QtWidgets import QComboBox, QLabel

from ..application.faults import FaultAction, FaultOperation, FaultRule
from .widgets import EditDialog, spinbox


class FaultDialog(EditDialog):
    def __init__(self, parent):
        super().__init__(parent, "设置故障注入", self._apply)
        self.pane = parent
        self.action = QComboBox()
        self.action.addItems(list(FaultAction))
        self.operation = QComboBox()
        self.operation.addItems(list(FaultOperation))
        self.scope = QComboBox()
        self.scope.addItems(("下一次", "指定次数", "持续时间"))
        self.count = spinbox(1, 10000, 3)
        self.duration = spinbox(1, 600000, 5000)
        self.duration.setSuffix(" ms")
        self.delay = spinbox(1, 60000, 500)
        self.delay.setSuffix(" ms")
        for title, widget in (("故障模板", self.action), ("匹配操作", self.operation), ("生效范围", self.scope),
                              ("次数", self.count), ("持续时间", self.duration), ("延迟", self.delay)):
            self.form.addRow(title, widget)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.form.addRow(self.description)
        self.action.currentIndexChanged.connect(self._refresh)
        self.scope.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        action = FaultAction(self.action.currentText())
        self.form.setRowVisible(self.count, self.scope.currentIndex() == 1)
        self.form.setRowVisible(self.duration, self.scope.currentIndex() == 2)
        self.form.setRowVisible(self.delay, action == FaultAction.DELAY)
        self.description.setText({
            FaultAction.DELAY: "先执行请求，再延迟发送应答。客户端超时后，写入可能已经生效。",
            FaultAction.EXCEPTION: "直接返回协议异常，跳过该请求的读写操作。",
            FaultAction.DISCONNECT: "关闭该客户端的连接，跳过该请求的读写操作。",
        }[action])

    def _apply(self):
        scope = self.scope.currentIndex()
        rule = FaultRule(FaultAction(self.action.currentText()), FaultOperation(self.operation.currentText()),
                         count=(1 if scope == 0 else self.count.value()) if scope != 2 else None,
                         duration_ms=self.duration.value() if scope == 2 else None, delay_ms=self.delay.value())
        self.pane.bridge.submit(self.pane.bridge.session.configure_fault(rule))
