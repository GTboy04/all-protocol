from PySide6.QtWidgets import QLabel, QVBoxLayout


def add_field(layout, caption, control, stretch=0):
    group = QVBoxLayout()
    group.setSpacing(4)
    label = QLabel(caption)
    label.setObjectName("fieldLabel")
    label.setBuddy(control)
    group.addWidget(label)
    group.addWidget(control)
    layout.addLayout(group, stretch)
