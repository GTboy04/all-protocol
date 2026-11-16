from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout


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


def point_sort_picker():
    picker = QComboBox()
    picker.setAccessibleName("点位排序")
    for caption, mode in (("自定义顺序", "created"), ("地址升序", "address_asc"), ("地址降序", "address_desc")):
        picker.addItem(caption, mode)
    return picker


def configure_point_reordering(table, move):
    """Use Qt's row-header drag gesture, committing moves to the underlying data."""
    header = table.verticalHeader()
    header.show()
    header.setFirstSectionMovable(True)
    header.setMinimumWidth(36)
    header.setAccessibleName("拖动行号调整点位顺序")

    def moved(_logical, old, new):
        # Keep visual and logical row indices identical. The owner then updates
        # its definitions, so saving, selection and live values use the new order.
        with QSignalBlocker(header):
            header.moveSection(new, old)
        move(old, new)

    header.sectionMoved.connect(moved)


def set_point_reordering_enabled(table, enabled, reason=""):
    header = table.verticalHeader()
    header.setSectionsMovable(enabled)
    header.setToolTip("按住左侧行号上下拖动，调整点位顺序；保存项目或方案后保留。" if enabled else reason)
    header.setCursor(Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor)


def reordered_points(points, visible, source, destination):
    """Move a visible row while keeping filtered-out definitions in their slots."""
    if source == destination or not (0 <= source < len(visible) and 0 <= destination < len(visible)):
        return tuple(points)
    ordered = list(visible)
    ordered.insert(destination, ordered.pop(source))
    visible_ids = {id(point) for point in visible}
    remaining = iter(ordered)
    return tuple(next(remaining) if id(point) in visible_ids else point for point in points)


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
