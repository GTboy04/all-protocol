import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QVBoxLayout, QWidget

from all_protocol.ui.journal_view import JournalView
from all_protocol.ui.main_window import Rows, _table


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.resize(1000, 600)
    model = Rows(("日志",), lambda row: (row,))
    table = _table(model)
    detail = QPlainTextEdit()
    view = JournalView(table, detail, "请求日志")
    QVBoxLayout(host).addWidget(view)
    host.show()
    app.processEvents()
    yield app, host, model, table, detail, view
    if view._dialog:
        view._dialog.close()
    host.close()
    host.deleteLater()
    app.processEvents()


def test_detached_reader_keeps_live_model_selection_and_restores_widgets(reader):
    app, _, model, table, detail, view = reader
    model.prepend("首条请求")
    table.selectRow(0)
    detail.setPlainText("RX " + "00 01 " * 200)
    view.expand_button.click()
    dialog = view._dialog
    app.processEvents()
    assert dialog.isVisible() and not dialog.isModal()
    assert dialog.isAncestorOf(table) and dialog.isAncestorOf(view.toolbar)
    assert table.currentIndex().data() == "首条请求"
    model.prepend("新请求")
    assert table.model().rowCount() == 2
    assert table.currentIndex().data() == "首条请求"
    view.open_window()
    assert view._dialog is dialog
    detail.setPlainText("实时 RX 更新")
    dialog.close()
    app.processEvents()
    assert view._dialog is None and view.isAncestorOf(table)
    assert view.isAncestorOf(view.toolbar) and view.toolbar.isVisible()
    assert detail.toPlainText() == "实时 RX 更新"
    assert table.currentIndex().data() == "首条请求"
    view.open_window()
    assert view._dialog.isVisible()


def test_long_packets_wrap_and_reader_can_change_orientation(reader):
    app, _, _, _, detail, view = reader
    detail.setPlainText("0123456789ABCDEF" * 200)
    app.processEvents()
    assert detail.horizontalScrollBar().maximum() == 0
    assert detail.verticalScrollBar().maximum() > 0
    view.wrap_button.click()
    app.processEvents()
    assert detail.horizontalScrollBar().maximum() > 0
    view.orientation_button.click()
    assert view.split.orientation() == Qt.Orientation.Horizontal
    view.orientation_button.click()
    assert view.split.orientation() == Qt.Orientation.Vertical


def test_shallow_reader_adapts_but_preserves_explicit_layout_choice(reader):
    app, host, _, _, _, view = reader
    host.resize(1100, 260)
    app.processEvents()
    assert view.split.orientation() == Qt.Orientation.Horizontal
    host.resize(1100, 700)
    app.processEvents()
    assert view.split.orientation() == Qt.Orientation.Vertical
    view.orientation_button.click()
    host.resize(1000, 600)
    app.processEvents()
    assert view.split.orientation() == Qt.Orientation.Horizontal
    view.expand_button.click()
    app.processEvents()
    assert view.split.orientation() == Qt.Orientation.Horizontal
    view.expand_button.click()
    app.processEvents()
    assert view._dialog is None and view.isAncestorOf(view.toolbar)
