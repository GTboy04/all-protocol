from PySide6.QtWidgets import QLabel, QSpinBox, QVBoxLayout


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
