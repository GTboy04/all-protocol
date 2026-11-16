"""A staged editor: cancelling either dialog leaves the saved point untouched."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem

from ..bit_definitions import BitDefinition, bit_width, validate_bit_definitions
from ..application.contracts import ClientError
from .widgets import EditDialog


class BitDefinitionsEditor(QPushButton):
    def __init__(self, kind, definitions=(), parent=None):
        super().__init__(parent)
        self.kind = kind
        self.definitions = definitions
        self.setAccessibleName("定义每个 Bit 的含义")
        self.clicked.connect(self._edit)
        self._caption()

    def _caption(self):
        self.setText(f"Bit 含义…（已定义 {len(self.definitions)} 位）")

    def _edit(self):
        try:
            kind = self.kind()
        except (ClientError, ValueError) as error:
            dialog = EditDialog(self, "Bit 含义", lambda: None)
            label = QLabel(str(error))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            dialog.form.addRow(label)
            dialog.exec()
            dialog.deleteLater()
            return
        width = bit_width(kind)
        if not width:
            cleared = []

            def commit_clear():
                if cleared:
                    self.definitions = ()
                    self._caption()

            dialog = EditDialog(self, "Bit 含义", commit_clear)
            dialog.form.addRow(QLabel("当前类型不支持位模式，请先选择数值或布尔类型。"))
            if self.definitions:
                clear = QPushButton("清空所有定义")
                def request_clear():
                    cleared.append(True)
                    clear.setText("已清空，点击确定后应用")
                clear.clicked.connect(request_clear)
                dialog.form.addRow(clear)
            dialog.exec()
            dialog.deleteLater()
            return
        rows = QTableWidget(width, 4)
        rows.setAccessibleName("逐位业务含义")
        rows.setHorizontalHeaderLabels(("Bit", "名称", "0 的含义", "1 的含义"))
        rows.verticalHeader().hide()
        rows.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        rows.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        rows.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        rows.setMinimumHeight(300)
        by_bit = {item.bit: item for item in self.definitions}
        outside = [item for item in self.definitions if item.bit >= width]
        for bit in range(width):
            item = by_bit.get(bit, BitDefinition(bit))
            for column, value in enumerate((str(bit), item.name, item.zero, item.one)):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                rows.setItem(bit, column, cell)

        def commit():
            definitions = []
            for bit in range(width):
                fields = [rows.item(bit, column).text() for column in (1, 2, 3)]
                if any(value.strip() for value in fields):
                    definitions.append(BitDefinition(bit, *fields))
            # Changing to a narrower type must never silently discard meanings.
            definitions.extend(outside)
            validate_bit_definitions(tuple(definitions), kind)
            self.definitions = tuple(definitions)
            self._caption()

        dialog = EditDialog(self, f"Bit 含义 · {kind} · {width} 位", commit)
        dialog.form.addRow(QLabel("bit 0 为最低位。填写需要的位，其余留空。例如：运行 / 停止 / 运行。"))
        dialog.form.addRow(rows)
        clear = QPushButton("清空所有定义")
        def clear_all():
            for bit in range(width):
                for column in (1, 2, 3):
                    rows.item(bit, column).setText("")
            # Out-of-range definitions are removed only on explicit clear + save.
            outside.clear()
        clear.clicked.connect(clear_all)
        dialog.form.addRow(clear)
        if outside:
            dialog.form.addRow(QLabel("已有定义超出当前位宽，请恢复原类型，或明确清空定义后重新填写。"))
        dialog.resize(720, 520)
        dialog.exec()
        dialog.deleteLater()
