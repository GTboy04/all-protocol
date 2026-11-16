from PySide6.QtWidgets import QCheckBox, QLabel

from ..application.recovery import RecoveryPolicy
from .widgets import EditDialog, spinbox


class RecoveryDialog(EditDialog):
    def __init__(self, parent, policy):
        super().__init__(parent, "断线恢复设置", self._commit)
        self.policy = policy
        self.enabled = QCheckBox("意外断线或连接失败后自动重连")
        self.enabled.setChecked(policy.enabled)
        self.restore = QCheckBox("恢复断线前的轮询与订阅")
        self.restore.setChecked(policy.restore_reads)
        self.interval = spinbox(200, 300000, policy.interval_ms)
        self.maximum = spinbox(200, 300000, policy.max_interval_ms)
        self.attempts = spinbox(1, 100, policy.max_attempts)
        for label, field in (("自动重连", self.enabled), ("恢复读取", self.restore),
                             ("初始间隔（ms）", self.interval), ("最大间隔（ms）", self.maximum),
                             ("重试上限（次）", self.attempts)):
            self.form.addRow(label, field)
        hint = QLabel("重试间隔逐次加倍，达到最大间隔后保持。主动断开和关闭项目会停止重试。\n"
                      "只恢复读取采集；写入、排队的手动请求和未知结果写入均不重放。\n"
                      "设置随设备保存，下次连接生效。")
        hint.setWordWrap(True)
        self.form.addRow(hint)

    def _commit(self):
        self.policy = RecoveryPolicy(self.enabled.isChecked(), self.interval.value(), self.maximum.value(),
                                     self.attempts.value(), self.restore.isChecked())
