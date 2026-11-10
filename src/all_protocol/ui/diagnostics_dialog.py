from datetime import datetime

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QSplitter,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..application.contracts import ErrorKind
from ..application.diagnostics import DiagnosticFilter, request_stats
from ..application.packet_decoder import decode_packet
from ..storage import VISIBLE_LOG_ROWS
from .main_window import Rows, _table
from .widgets import add_field


def _row(item):
    _, row = item
    return (row.time.strftime("%m-%d %H:%M:%S.%f")[:-3], row.source, row.operation, row.target,
            row.function or "—", f"{row.elapsed_ms} ms" if row.elapsed_ms is not None else "—", row.outcome)


class DiagnosticsDialog(QDialog):
    def __init__(self, parent, entries, describe, *, server=False, selected=None, device=None):
        super().__init__(parent)
        self.setWindowTitle("请求诊断 · 服务端" if server else "请求诊断 · 客户端")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 670)
        self._entries = entries
        self._describe = describe
        self._snapshot = ()
        self._rows = {}
        self._devices = {}
        self._visible = ()
        self._selected = None
        self._hex_columns = 16

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.device = QComboBox()
        self.device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device.setMinimumContentsLength(16)
        self.window = QComboBox()
        self.window.addItem("全部保留记录", 0)
        for minutes in (1, 5, 15, 60):
            self.window.addItem(f"最近 {minutes} 分钟", minutes)
        self.operation = QComboBox()
        self.operation.addItems(("全部", "读取", "写入", "连接", "监听", "其他") if server else
                                ("全部", "读取", "写入", "连接", "断开"))
        self.error = QComboBox()
        self.error.addItem("全部", None)
        self.error.addItem("有错误", True)
        self.error.addItem("无错误", False)
        for kind in ErrorKind:
            self.error.addItem(str(kind), kind.value)
        for title, combo in (("来源客户端" if server else "设备", self.device), ("时间窗口", self.window),
                             ("操作", self.operation), ("错误", self.error)):
            add_field(controls, title, combo, 1)
            combo.currentIndexChanged.connect(self._refresh)
        layout.addLayout(controls)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.scope = QLabel(f"基于最近 {VISIBLE_LOG_ROWS} 条保留记录。统计仅含筛选后的读写，剔除输入错误和未发送的取消项。"
                            "缺失耗时的旧记录仅计数量。P50、P95 取最近秩。")
        self.scope.setWordWrap(True)
        layout.addWidget(self.stats)
        layout.addWidget(self.scope)

        self.model = Rows(("时间", "设备 / 来源", "操作", "目标", "功能码", "耗时", "结果"), _row)
        self.table = _table(self.model)
        self.table.setAccessibleName("筛选后的请求")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 165)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().currentRowChanged.connect(self._select_row)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        self.context = QLabel("选择一条请求查看报文")
        self.context.setTextFormat(Qt.TextFormat.PlainText)
        self.context.setWordWrap(True)
        self.context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.context)
        self.packet = QComboBox()
        self.packet.setAccessibleName("捕获的报文")
        self.packet.currentIndexChanged.connect(self._show_packet)
        detail_layout.addWidget(self.packet)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("协议字段")
        self.tree.setHeaderLabels(("协议字段", "值", "偏移（十进制）"))
        self.tree.setColumnWidth(0, 180)
        self.tree.setColumnWidth(1, 140)
        self.tree.currentItemChanged.connect(self._highlight)
        self.hex = QPlainTextEdit()
        self.hex.setAccessibleName("原始十六进制")
        self.hex.setObjectName("packetDetail")
        self.hex.setFont(QFont("Consolas"))
        self.hex.setReadOnly(True)
        self.hex.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        inspection = QSplitter(Qt.Orientation.Horizontal)
        inspection.addWidget(self.tree)
        inspection.addWidget(self.hex)
        inspection.setSizes([430, 500])
        detail_layout.addWidget(inspection, 1)
        split.addWidget(detail)
        split.setSizes([235, 330])
        layout.addWidget(split, 1)

        self._refresh()
        if device is not None:
            self._select_device(device)
        for index, (key, _) in enumerate(self.model.items):
            if key == id(selected):
                self.table.setCurrentIndex(self.model.index(index, 0))
                break
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(500)

    def _select_device(self, device):
        index = next((index for index in range(self.device.count()) if self.device.itemData(index) == device), 0)
        self.device.setCurrentIndex(index)

    def _refresh(self):
        if not hasattr(self, "model"):
            return
        entries = tuple(self._entries())
        keys = tuple(map(id, entries))
        changed = keys != tuple(map(id, self._snapshot))
        if changed or not self.device.count():
            rows = {}
            devices = {}
            for key, entry in zip(keys, entries, strict=True):
                row = self._rows.get(key)
                if row is None:
                    row = self._describe(entry)
                rows[key] = row
                devices.setdefault(row.device, row.source)
            self._rows = rows
            self._snapshot = entries
            selected = self.device.currentData()
            if selected is not None:
                devices.setdefault(selected, self.device.currentText())
            if devices != self._devices or not self.device.count():
                self._devices = devices
                with QSignalBlocker(self.device):
                    self.device.clear()
                    self.device.addItem("全部", None)
                    for device, name in sorted(devices.items(), key=lambda item: (item[1], item[0])):
                        self.device.addItem(name, device)
                    self._select_device(selected)
        minutes = self.window.currentData()
        now = datetime.now().timestamp()
        criteria = DiagnosticFilter(self.device.currentData(), now - minutes * 60 if minutes else None,
                                    now if minutes else None,
                                    self.operation.currentText() if self.operation.currentIndex() else "",
                                    self.error.currentData())
        visible = tuple(key for key in keys if criteria.matches(self._rows[key]))
        if visible != self._visible:
            current = self._selected
            self._visible = visible
            with QSignalBlocker(self.table.selectionModel()):
                self.model.reset(tuple((key, self._rows[key]) for key in visible))
                if current in visible:
                    self.table.setCurrentIndex(self.model.index(visible.index(current), 0))
            if current not in visible:
                self._select_row(self.table.currentIndex())
        stats = request_stats(self._rows[key] for key in visible)
        failure = f"{stats.failed / stats.count:.1%}（{stats.failed}）" if stats.count else "—"
        latency = (f"{stats.average_ms:.1f} / {stats.p50_ms} / {stats.p95_ms} ms"
                   if stats.durations else "—")
        self.stats.setText(f"{self.window.currentText()} · 匹配 {len(visible)} 条 · 读写 {stats.count} 次 · 失败 {failure}"
                           f" · 写入结果未知 {stats.unknown_writes} 次\n"
                           f"平均 / P50 / P95：{latency}（{stats.durations} 条有耗时）")

    def _select_row(self, index, _previous=None):
        self._selected, row = self.model.items[index.row()] if index.isValid() else (None, None)
        with QSignalBlocker(self.packet):
            self.packet.clear()
            if row:
                for number, (label, _, data) in enumerate(row.packets, 1):
                    self.packet.addItem(f"{number} · {label} · {len(data)} 字节")
        self.context.setText(" · ".join(filter(None, (row.identity, row.protocol, row.source, row.operation,
                                                      row.target, row.outcome,
                                                      row.error if row.error != row.outcome else "", row.detail)))
                             if row else "选择一条请求查看报文")
        self._show_packet()

    def _show_packet(self, _index=None):
        self.tree.clear()
        self.hex.clear()
        index = self.packet.currentIndex()
        if self._selected is None or index < 0:
            return
        row = self._rows[self._selected]
        _, request, data = row.packets[index]
        columns = self._hex_columns
        self.hex.setPlainText("\n".join(f"{offset:08X}  {data[offset:offset + columns].hex(' ').upper()}"
                                       for offset in range(0, len(data), columns)))
        for field in decode_packet(row.protocol, data, request=request):
            self._add_field(self.tree, field)
        self.tree.expandToDepth(1)

    def _add_field(self, parent, field):
        offsets = ", ".join(str(start) if end == start + 1 else f"{start}–{end - 1}" for start, end in field.spans)
        item = QTreeWidgetItem(parent, (field.name, field.value, offsets))
        item.setData(0, Qt.ItemDataRole.UserRole, field.spans)
        for child in field.children:
            self._add_field(item, child)

    def _highlight(self, item, _previous=None):
        spans = item.data(0, Qt.ItemDataRole.UserRole) if item else ()
        selections = []
        document = self.hex.document()
        columns = self._hex_columns
        style = QTextCharFormat()
        style.setBackground(QColor("#f4cc70"))
        style.setForeground(QColor("#182031"))
        for start, end in spans or ():
            while start < end:
                line, offset = divmod(start, columns)
                size = min(end - start, columns - offset)
                cursor = QTextCursor(document.findBlockByNumber(line))
                begin = cursor.position() + 10 + offset * 3
                cursor.setPosition(begin)
                cursor.setPosition(begin + size * 3 - 1, QTextCursor.MoveMode.KeepAnchor)
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = style
                selections.append(selection)
                start += size
        if selections:
            cursor = QTextCursor(selections[0].cursor)
            cursor.setPosition(cursor.selectionStart())
            self.hex.setTextCursor(cursor)
            self.hex.ensureCursorVisible()
        self.hex.setExtraSelections(selections)
