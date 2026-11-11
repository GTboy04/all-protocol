"""Check incremental native painting, which QWidget.grab() would repaint/mask."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget

from all_protocol.ui.glass import acrylic_supported, apply_backdrop
from all_protocol.ui.style import apply_windows_theme, stylesheet
from all_protocol.ui.widgets import EditDialog


def test_backdrop_clears_old_text_and_restores_background_on_fallback():
    # The native backing store is essential to reproducing this Windows bug.
    # Isolate QApplication/style initialization from other desktop tests.
    platform = "windows" if sys.platform == "win32" else "offscreen"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        env={**os.environ, "QT_QPA_PLATFORM": platform},
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _check_fallbacks():
    for factory in (EditDialog, QMainWindow, QWidget):
        window = factory(None, "Repaint check", lambda: None) if factory is EditDialog else factory()
        try:
            # Unsupported platform must also clear a previously active backdrop.
            window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            window.setProperty("acrylic", True)
            with patch("all_protocol.ui.glass.acrylic_supported", return_value=False):
                assert not apply_backdrop(window, enabled=True, dark=False)
            assert not window.property("acrylic")
            assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert not window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

            for result in (0, -1):
                dwm = SimpleNamespace(DwmSetWindowAttribute=Mock(return_value=result),
                                      DwmExtendFrameIntoClientArea=Mock(return_value=result))
                with patch("all_protocol.ui.glass.acrylic_supported", return_value=True), \
                        patch("all_protocol.ui.glass.ctypes.WinDLL", return_value=dwm, create=True):
                    assert apply_backdrop(window, enabled=True, dark=False) == (result == 0)
                    assert not window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
                    assert not apply_backdrop(window, enabled=False, dark=False)
                    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                    assert not window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

            with patch("all_protocol.ui.glass.acrylic_supported", return_value=True), \
                    patch("all_protocol.ui.glass.ctypes.WinDLL", side_effect=OSError, create=True):
                assert not apply_backdrop(window, enabled=True, dark=False)
            assert not window.property("acrylic")
            assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert not window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        finally:
            window.close()
            window.destroy()


def _check_native_repainting(app):
    if not acrylic_supported():
        print("Native Acrylic unavailable; fallback checks passed")
        return
    for dark in (False, True):
        app.setStyleSheet(stylesheet(dark))
        dialog = EditDialog(None, "Dialog repaint regression", lambda: None)
        address = QLabel("")
        address.setFixedSize(220, 32)
        dialog.error.setFixedSize(220, 32)
        dialog.form.addRow("绝对地址", address)
        try:
            dialog.show()
            QTest.qWait(200)
            assert dialog.property("acrylic")

            def pixels(label=address):
                # Read the displayed native backing store without requesting a
                # fresh Qt render, and keep coordinates in logical DPI units.
                pos = label.mapTo(dialog, label.rect().topLeft())
                return dialog.screen().grabWindow(
                    int(dialog.winId()), pos.x(), pos.y(), label.width(), label.height(),
                ).toImage()

            blank = pixels()
            assert not blank.isNull()
            blank_error = pixels(dialog.error)
            address.setText("IW0")
            QTest.qWait(30)
            current = pixels()
            assert current != blank
            for _ in range(3):
                for text in ("DB1.DBW0", "MW0", "IW0", "", "DB65535.DBX123.7", "QW0", "IW0"):
                    address.setText(text)
                    QTest.qWait(30)
                assert pixels() == current, "Previous addresses overlapped the current address"
                address.clear()
                QTest.qWait(30)
                assert pixels() == blank, "Old label pixels remained after clearing text"

            # Error messages use the same transparent QLabel painting path.
            dialog.commit = Mock(side_effect=ValueError("地址配置错误"))
            dialog.accept()
            QTest.qWait(30)
            assert pixels(dialog.error) != blank_error
            dialog.error.clear()
            QTest.qWait(30)
            assert pixels(dialog.error) == blank_error
        finally:
            dialog.close()
            dialog.destroy()
    print("Light/dark native incremental repaint checks passed")


if __name__ == "__main__":
    app = QApplication([])
    theme = apply_windows_theme(app)
    _check_fallbacks()
    _check_native_repainting(app)
