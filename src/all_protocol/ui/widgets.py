from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout


class EditDialog(QDialog):
    def __init__(self, parent, title, commit):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(350)
        self.commit = commit
        layout = QVBoxLayout(self)
        self.form = QFormLayout()
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
