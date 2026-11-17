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


def test_pause_freezes_selection_scroll_and_detached_reader(reader):
    app, _, model, table, detail, view = reader
    model.reset(tuple(f"请求 {i}" for i in range(100)))
    table.selectRow(40)
    detail.setPlainText("正在阅读的报文")
    app.processEvents()
    scroll = table.verticalScrollBar().value()
    view.pause_button.click()
    model.prepend_many(("新请求 2", "新请求 1"))
    assert model.rowCount() == 100
    assert table.currentIndex().data() == "请求 40"
    assert table.verticalScrollBar().value() == scroll
    assert detail.toPlainText() == "正在阅读的报文"
    view.open_window()
    assert view.paused and view.pause_button.text() == "恢复输出"
    view._dialog.close()
    view.pause_button.click()
    assert model.items[:2] == ["新请求 2", "新请求 1"]
    assert table.currentIndex().data() == "请求 40"
    assert not view.paused


def test_pause_retains_only_latest_rows_and_clears_expired_selection(reader):
    from all_protocol.storage import VISIBLE_LOG_ROWS
    _, _, model, table, detail, view = reader
    model.prepend("旧请求")
    table.selectRow(0)
    detail.setPlainText("旧报文")
    view.pause_button.click()
    for i in range(VISIBLE_LOG_ROWS + 10):
        model.prepend(str(i))
    assert model.items == ["旧请求"]
    view.pause_button.click()
    assert model.rowCount() == VISIBLE_LOG_ROWS
    assert model.items[0] == str(VISIBLE_LOG_ROWS + 9)
    assert not table.currentIndex().isValid()
    assert detail.toPlainText() == ""


def test_rotation_is_deferred_and_explicit_clear_discards_paused_backlog(reader):
    _, _, model, _, _, view = reader
    model.prepend("旧请求")
    view.pause_button.click()
    model.reset()
    model.prepend("轮换后请求")
    assert model.items == ["旧请求"]
    view.pause_button.click()
    assert model.items == ["轮换后请求"]
    view.pause_button.click()
    model.prepend("待显示请求")
    model.reset(force=True)
    assert model.items == []
    model.prepend("清空后请求")
    assert model.items == []
    view.pause_button.click()
    assert model.items == ["清空后请求"]
