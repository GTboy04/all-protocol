"""Exercise the real application theme and keyboard entry points without network IO."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QPushButton, QScrollArea, QStyle, QStyleOptionSpinBox

from all_protocol.ui.role_window import RoleWindow
from all_protocol.ui.style import apply_windows_theme
from all_protocol.ui.widgets import spinbox
from all_protocol.application.binary import bit_pattern
from all_protocol.ui.bit_inspector import BitReading, show_bit_inspector


def test_system_theme_switch_and_compact_keyboard_navigation(tmp_path):
    # Changing the global Qt style with old windows from other test modules
    # still awaiting collection can crash Windows' native style engine. The
    # application initializes it once, before creating any widgets; do likewise.
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def _check_desktop(tmp_path):
    app = QApplication.instance() or QApplication([])
    previous_font, previous_sheet = app.font(), app.styleSheet()
    previous_scheme = app.styleHints().colorScheme()
    theme = apply_windows_theme(app)
    home = RoleWindow((), tmp_path, server_protocols=())
    try:
        home.resize(640, 480)
        home.show()
        home.activateWindow()
        QTest.qWait(20)
        scroll = home.findChild(QScrollArea)
        assert scroll.widgetResizable()
        assert scroll.horizontalScrollBar().maximum() == 0
        # Every entry remains keyboard reachable, including below the fold.
        server = next(b for b in home.findChildren(QPushButton) if b.text() == "进入服务端")
        home.project_buttons["新建项目"].setFocus()
        for _ in range(12):
            if home.focusWidget() is server:
                break
            QTest.keyClick(home.focusWidget(), Qt.Key.Key_Tab)
        assert home.focusWidget() is server
        assert scroll.viewport().rect().contains(server.mapTo(scroll.viewport(), server.rect().center()))

        # A real keyboard shortcut follows the same project creation path.
        with patch.object(QInputDialog, "getText", lambda *_: ("键盘项目", True)):
            QTest.keyClick(home, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
            QTest.qWait(150)  # Shortcuts animate the button before clicking.
        assert home.workspace.project.name == "键盘项目"

        # Rounded step buttons retain distinct hit targets and keyboard access.
        box = spinbox(0, 100, 10)
        box.setParent(home)
        box.setGeometry(24, 80, 130, 36)
        box.show()
        app.processEvents()
        option = QStyleOptionSpinBox()
        box.initStyleOption(option)
        up = box.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                       QStyle.SubControl.SC_SpinBoxUp, box)
        down = box.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                         QStyle.SubControl.SC_SpinBoxDown, box)
        assert not up.intersects(down)
        assert box.rect().contains(up) and box.rect().contains(down)
        QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=up.center())
        assert box.value() == 11
        QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=down.center())
        assert box.value() == 10
        QTest.keyClick(box, Qt.Key.Key_Up)
        assert box.value() == 11
        box.hide()

        # Updating a selected scalar refreshes bits; clearing it removes old bits.
        reading = [BitReading("状态字", bit_pattern(0x8001, "UInt16"), "有效")]
        show_bit_inspector(home, lambda: reading[0])
        inspector = home._bit_inspector
        assert len(inspector.cells) == 16
        assert inspector.cells[0].text() == "bit 15\n1"
        assert inspector.cells[-1].text() == "bit 0\n1"
        inspector._select(15)
        assert "0x8000" in inspector.detail.text()
        reading[0] = BitReading("负数", bit_pattern(-1, "Int32"), "断开；旧数据")
        QTest.qWait(250)
        assert len(inspector.cells) == 32
        assert "旧数据" in inspector.context.text()
        assert all(cell.property("bitValue") == "1" for cell in inspector.cells)
        reading[0] = None
        inspector.refresh()
        assert not inspector.cells and not inspector.copy.isEnabled()
        inspector.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        # Rendering and disabled button contrast follow both application themes.
        button = home.project_buttons["打开项目…"]
        for dark in (False, True):
            scheme = Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
            app.styleHints().setColorScheme(scheme)
            QTest.qWait(20)
            # Offscreen platforms may reject OS scheme overrides. Check the
            # effective scheme, as the application does, rather than the hint.
            effective_dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
            home.grab()
            background = button.palette().color(QPalette.ColorRole.Button)
            assert (background.lightness() < 128) == effective_dark
            button.setEnabled(False)
            home.grab()
            foreground = button.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)
            assert foreground != button.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button)
            button.setEnabled(True)
        if hasattr(app.styleHints(), "accessibility"):
            from types import SimpleNamespace
            with patch.object(app.styleHints(), "accessibility", lambda: SimpleNamespace(
                    contrastPreference=lambda: Qt.ContrastPreference.HighContrast)):
                theme.apply()
                assert not app.styleSheet()  # Let Windows supply all contrast colors.
                assert app.property("highContrast")
    finally:
        home.workspace = None
        home.close()
        home.deleteLater()
        app.removeEventFilter(theme)
        theme.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        del app.windows_theme
        app.styleHints().setColorScheme(previous_scheme)
        app.setProperty("highContrast", False)
        app.setFont(previous_font)
        app.setStyleSheet(previous_sheet)


if __name__ == "__main__":
    _check_desktop(Path(sys.argv[1]))
