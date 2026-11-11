from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout


class EditDialog(QDialog):
    def __init__(self, parent, title, commit):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(350)
        self.commit = commit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.form = QFormLayout()
        self.form.setSpacing(12)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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


def spinbox(minimum, maximum, value):
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    box.setKeyboardTracking(False)
    # Native spin-box hints include oversized step controls. Reserve enough for
    # the value, a unit suffix and our small arrows without crowding host fields.
    digits = max(box.fontMetrics().horizontalAdvance(str(bound)) for bound in (minimum, maximum))
    box.setMaximumWidth(max(96, digits + 80))
    return box


def add_field(layout, caption, control, stretch=0):
    group = QVBoxLayout()
    group.setSpacing(4)
    label = QLabel(caption)
    label.setObjectName("fieldLabel")
    label.setBuddy(control)
    group.addWidget(label)
    group.addWidget(control)
    layout.addLayout(group, stretch)
