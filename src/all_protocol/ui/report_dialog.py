from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from ..reporting import ReportCancellation, ReportScope, export_report, prepare_report


class ReportDialog(QDialog):
    def __init__(self, parent, capture):
        super().__init__(parent)
        self.setWindowTitle("导出联调报告")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        available = parent.screen().availableGeometry()
        self.resize(min(870, available.width() - 40), min(660, available.height() - 50))
        self._capture = capture
        self._bundle = None
        self._future = None
        self._job = None
        self._cancel = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="protocol-report")
        self.finished.connect(self._shutdown_worker)
        self._timer = QTimer(self, interval=50)
        self._timer.timeout.connect(self._poll)

        self.scope = QWidget()
        settings = QFormLayout(self.scope)
        settings.setContentsMargins(0, 0, 0, 0)
        now = QDateTime.currentDateTime()
        self.start = QDateTimeEdit(now.addSecs(-3600))
        self.end = QDateTimeEdit(now)
        for edit in (self.start, self.end):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setCalendarPopup(True)
            edit.dateTimeChanged.connect(self._invalidate)
        self.end_at_freeze = QCheckBox("截至冻结时")
        self.end_at_freeze.setChecked(True)
        self.end_at_freeze.toggled.connect(self._end_changed)
        self.end.setEnabled(False)
        end_row = QHBoxLayout()
        end_row.addWidget(self.end)
        end_row.addWidget(self.end_at_freeze)
        end_row.addStretch()
        self.clients = QCheckBox("项目设备")
        self.servers = QCheckBox("本次工作区模拟服务")
        self.packets = QCheckBox("包含原始 TX / RX 报文")
        self.clients.setChecked(True)
        self.servers.setChecked(True)
        sources = QHBoxLayout()
        sources.addWidget(self.clients)
        sources.addWidget(self.servers)
        sources.addStretch()
        for option in (self.clients, self.servers, self.packets):
            option.toggled.connect(self._invalidate)
        settings.addRow("开始时间", self.start)
        settings.addRow("结束时间", end_row)
        settings.addRow("日志来源", sources)
        settings.addRow("附加数据", self.packets)
        settings.addRow(QLabel(f"本机时区 {datetime.now().astimezone():%Z (%z)}。预览与导出使用同一快照。"))

        self.pages = QComboBox()
        self.pages.setAccessibleName("报告预览页")
        self.pages.currentIndexChanged.connect(self._show_page)
        self.preview = QTextBrowser()
        self.preview.setAccessibleName("联调报告预览")
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(self._locate_record)
        self.preview.setPlaceholderText("选择范围后生成预览，可查看将导出的设备地址、日志和报文。")
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.generate_button = QPushButton("冻结并预览")
        self.generate_button.clicked.connect(self._generate)
        self.export_button = QPushButton("保存报告包…")
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self._export)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self._cancel_job)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addWidget(self.generate_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        actions.addWidget(self.export_button)
        actions.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.scope)
        layout.addWidget(self.pages)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.status)
        layout.addLayout(actions)
        self._refresh()

    def _end_changed(self, checked):
        self.end.setEnabled(not checked)
        self._invalidate()

    def _invalidate(self, *_):
        self._bundle = None
        self.pages.clear()
        self.preview.clear()
        self.status.clear()
        self._refresh()

    def _refresh(self):
        busy = self._future is not None
        self.scope.setEnabled(not busy)
        self.generate_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and self._bundle is not None)
        self.cancel_button.setEnabled(busy)
        self.pages.setEnabled(self._bundle is not None)

    def _generate(self):
        frozen_at = datetime.now().astimezone()
        start = datetime.fromtimestamp(self.start.dateTime().toMSecsSinceEpoch() / 1000, timezone.utc)
        end = frozen_at if self.end_at_freeze.isChecked() else datetime.fromtimestamp(
            self.end.dateTime().toMSecsSinceEpoch() / 1000, timezone.utc,
        )
        self._invalidate()
        try:
            scope = ReportScope(start, end, self.clients.isChecked(), self.servers.isChecked(), self.packets.isChecked())
            snapshot = self._capture(scope, frozen_at)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self._submit("preview", prepare_report, snapshot)

    def _export(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存联调报告包", "联调报告.zip", "报告包 (*.zip)")
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() != ".zip":
            self.status.setText("请使用 .zip 扩展名")
            return
        self._destination = path
        self._submit("export", export_report, self._bundle, path)

    def _submit(self, job, work, *args):
        self._job = job
        self._cancel = ReportCancellation()
        self._future = self._executor.submit(work, *args, self._cancel)
        self._timer.start()
        self.status.setText("正在生成预览…" if job == "preview" else "正在写入报告包…")
        self._refresh()

    def _cancel_job(self):
        if self._cancel is not None:
            accepted = self._cancel.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在取消…" if accepted else "正在完成文件替换…")

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        self._timer.stop()
        future, self._future = self._future, None
        try:
            result = future.result()
        except CancelledError:
            self.status.setText("任务已取消")
        except Exception as error:
            self.status.setText(f"报告任务失败 · {error}")
        else:
            if self._job == "preview":
                self._bundle = result
                self.pages.addItems([page.title for page in result.pages])
                self._show_page(self.pages.currentIndex())
                self.status.setText(f"预览已冻结 · {result.record_count} 条记录")
            else:
                self.status.setText(f"报告包已保存 · {self._destination}")
        self._cancel = None
        self._refresh()

    def _show_page(self, index):
        if self._bundle is not None and index >= 0:
            self.preview.setHtml(self._bundle.pages[index].html)

    def _locate_record(self, url):
        anchor = url.fragment()
        index = anchor.removeprefix("record-")
        if self._bundle is None or not anchor.startswith("record-") or not index.isdecimal():
            return
        for page_index in reversed(range(len(self._bundle.pages))):
            first = self._bundle.pages[page_index].first_record
            if first and first <= int(index):
                self.pages.setCurrentIndex(page_index)
                self.preview.scrollToAnchor(anchor)
                break

    def _shutdown_worker(self):
        self._timer.stop()
        if self._cancel is not None:
            self._cancel.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
