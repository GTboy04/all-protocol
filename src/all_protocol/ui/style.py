"""Fluent-inspired surfaces over native Qt controls; Qt owns the system palette."""
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QStyleFactory, QDialog, QMainWindow, QWidget

from .glass import acrylic_supported, apply_backdrop

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def colors(dark=False):
    if dark:
        return dict(canvas="#202631", glass_canvas="rgba(26, 33, 44, 180)", surface="rgba(48, 58, 72, 195)", subtle="rgba(53, 64, 78, 170)",
                    text="#f5f8fc", secondary="#c0cad7", border="rgba(200, 220, 245, 38)",
                    hover="rgba(98, 119, 146, 95)", pressed="rgba(110, 134, 166, 115)", accent="#80cfff",
                    accent_hover="#8bdcff", on_accent="#003449", selection="#304653",
                    disabled="#a0a0a0", success="#6ccb5f", success_bg="#263b28",
                    warning="#fce100", warning_bg="#423d20")
    return dict(canvas="#edf3fa", glass_canvas="rgba(233, 242, 251, 180)", surface="rgba(255, 255, 255, 196)", subtle="rgba(243, 248, 253, 180)",
                text="#1b2b40", secondary="#55677b", border="rgba(96, 122, 153, 46)",
                hover="rgba(255, 255, 255, 235)", pressed="rgba(213, 228, 245, 210)", accent="#1269b4",
                accent_hover="#0055a5", on_accent="#ffffff", selection="#e5f0fa",
                disabled="#767676", success="#0f6b21", success_bg="#dff6dd",
                warning="#795000", warning_bg="#fff4ce")


_TEMPLATE = """
QWidget { color: @text; }
QWidget#workspace, QDialog { background: @canvas; }
QWidget#workspace[acrylic="true"], QDialog[acrylic="true"] { background: @glass_canvas; }
QMainWindow { background: transparent; }
QMainWindow[acrylic="true"] QWidget#workspace { background: transparent; }
QMainWindow[acrylic="true"] > QWidget#workspace { background: @glass_canvas; }
QWidget#workspace[acrylic="true"] QWidget#workspace { background: transparent; }
QScrollArea, QWidget#qt_scrollarea_viewport { background: transparent; }
QFrame#masthead { background: @subtle; border: 0; }
QLabel#title { font-size: 20px; font-weight: 600; }
QLabel#pageTitle { font-size: 28px; font-weight: 600; }
QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
QLabel#fieldLabel, QLabel#resultContext, QLabel#feedback { color: @secondary; }
QLabel#warning { color: @warning; }
QFrame#panel { background: @surface; border: 1px solid @border; border-radius: 12px; }
QLabel#permissions { color: @secondary; background: @subtle; border-radius: 4px; padding: 8px; }
QLabel#connectionStatus, QLabel#listenerStatus {
    color: @secondary; background: @subtle; border: 1px solid @border;
    border-radius: 4px; padding: 4px 8px;
}
QLabel#connectionStatus[tone="online"], QLabel#listenerStatus[tone="online"] {
    color: @success; background: @success_bg; border-color: @success_bg;
}
QLabel#connectionStatus[tone="pending"], QLabel#listenerStatus[tone="pending"] {
    color: @warning; background: @warning_bg; border-color: @warning_bg;
}
QPushButton {
    background: @surface; border: 1px solid @border;
    border-radius: 8px; min-height: 24px; padding: 4px 14px;
}
QPushButton:hover { background: @hover; }
QPushButton:pressed { background: @pressed; }
QPushButton:checked { background: @selection; border-color: @accent; }
QPushButton#primary:enabled { background: @accent; border-color: @accent; color: @on_accent; }
QPushButton#primary:enabled:hover { background: @accent_hover; }
QPushButton#primary:enabled:pressed { background: @accent; }
QPushButton:default { border-color: @accent; }
QPushButton:focus { outline: none; border: 2px solid @accent; padding: 3px 13px; }
QPushButton#primary:focus { border: 2px solid @on_accent; }
QPushButton:disabled { color: @disabled; background: @subtle; border-color: @border; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: @surface; color: @text; border: 1px solid @border;
    border-radius: 8px;
    selection-background-color: @accent; selection-color: @on_accent;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { min-height: 24px; padding: 4px 10px; }
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit { border: 0; padding: 0; background: transparent; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus { border-color: @accent; }
QSpinBox, QDoubleSpinBox { padding-right: 32px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; height: 12px; right: 4px; top: 4px;
    background: @subtle; border: 0; border-radius: 5px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; height: 12px; right: 4px; bottom: 4px;
    background: @subtle; border: 0; border-radius: 5px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: @hover; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed,
QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed { background: @pressed; }
QSpinBox::up-button:disabled, QSpinBox::down-button:disabled,
QDoubleSpinBox::up-button:disabled, QDoubleSpinBox::down-button:disabled { background: transparent; border-color: transparent; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("@assets/chevron-up.svg"); width: 8px; height: 5px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("@assets/chevron-down.svg"); width: 8px; height: 5px; }
QComboBox { padding-right: 28px; }
QComboBox::drop-down { border: 0; width: 24px; background: transparent; }
QComboBox::down-arrow { image: url("@assets/chevron-down.svg"); width: 8px; height: 5px; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    color: @disabled; background: @subtle; border-color: @border;
}
QComboBox QAbstractItemView {
    background: @surface; color: @text; border: 1px solid @border;
    selection-background-color: @selection; selection-color: @text;
}
QPlainTextEdit, QTextEdit { padding: 8px; }
QPlainTextEdit#packetDetail { background: @subtle; font-family: "Consolas", "Microsoft YaHei UI"; }
QTableView, QListWidget, QTreeWidget {
    background: @surface; alternate-background-color: @subtle; border: 1px solid @border;
    selection-background-color: @selection; selection-color: @text; border-radius: 8px;
}
QTableView:focus, QListWidget:focus, QTreeWidget:focus { border-color: @accent; }
QTableView::item { padding: 0 8px; }
QTreeWidget#projectTree { background: @canvas; border: 0; border-right: 1px solid @border; }
QTreeWidget::item, QListWidget::item { min-height: 28px; padding: 4px; }
QTreeWidget::item:hover, QListWidget::item:hover { background: @hover; }
QTreeWidget::item:selected, QListWidget::item:selected { background: @selection; color: @text; }
QHeaderView { background: @subtle; }
QHeaderView::section {
    background: @subtle; color: @secondary; border: 0;
    border-bottom: 1px solid @border; padding: 8px; font-weight: 600;
}
QTabWidget::pane { border: 0; }
QTabBar::tab {
    background: transparent; color: @secondary; border: 0;
    border-radius: 8px; padding: 7px 12px; margin: 4px;
}
QTabBar::tab:hover { background: @hover; }
QTabBar::tab:selected { color: @accent; background: @selection; }
QTabBar::tab:focus { background: @selection; }
QSplitter::handle { background: @canvas; }
QSplitter::handle:hover { background: @border; }
QStatusBar { background: @canvas; color: @secondary; }
QToolTip { background: @surface; color: @text; border: 1px solid @border; padding: 4px 8px; }
QLabel#binaryValue { background: @subtle; border-radius: 8px; padding: 12px; font-family: "Consolas"; }
QPushButton#bitCell { min-width: 40px; min-height: 40px; padding: 4px; font-family: "Consolas"; }
QPushButton#bitCell[bitValue="1"] { background: @selection; color: @accent; border-color: @accent; }
"""


def stylesheet(dark=False):
    result = _TEMPLATE
    palette = colors(dark)
    for name in sorted(palette, key=len, reverse=True):
        result = result.replace("@" + name, palette[name])
    if dark:
        result = result.replace("chevron-up.svg", "chevron-up-light.svg").replace("chevron-down.svg", "chevron-down-light.svg")
    return result.replace("@assets", ASSETS.as_posix())


STYLE = stylesheet()


def status_color(widget, tone="secondary"):
    app = QApplication.instance()
    if app is not None and app.property("highContrast"):
        return widget.palette().color(widget.foregroundRole())
    dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark if app else False
    return QColor(colors(dark)[tone])


class WindowsTheme(QObject):
    changed = Signal()

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self._refresh = QTimer(self, singleShot=True, interval=0)
        self._refresh.timeout.connect(self.apply)
        app.installEventFilter(self)
        hints = app.styleHints()
        hints.colorSchemeChanged.connect(self._schedule_refresh)
        if hasattr(hints, "accessibility"):
            hints.accessibility().contrastPreferenceChanged.connect(self._schedule_refresh)
        self.apply()

    def _schedule_refresh(self, *_):
        self._refresh.start()

    def eventFilter(self, watched, event):
        if watched is self.app and event.type() == QEvent.Type.ApplicationPaletteChange:
            self._refresh.start()
        if event.type() == QEvent.Type.Polish and isinstance(watched, QWidget) and watched.isWindow():
            if (isinstance(watched, (QDialog, QMainWindow)) or watched.objectName() == "workspace"):
                if not watched.property("glassInitialized"):
                    watched.setProperty("glassInitialized", True)
                    self._backdrop(watched)
        return False

    def _backdrop(self, window):
        enabled = not self.app.property("highContrast") and acrylic_supported()
        apply_backdrop(window, enabled=enabled,
                       dark=self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark)

    def apply(self):
        hints = self.app.styleHints()
        high_contrast = (hasattr(hints, "accessibility") and
                         hints.accessibility().contrastPreference() == Qt.ContrastPreference.HighContrast)
        self.app.setProperty("highContrast", high_contrast)
        self.app.setStyleSheet("" if high_contrast else stylesheet(hints.colorScheme() == Qt.ColorScheme.Dark))
        for window in self.app.topLevelWidgets():
            if window.property("glassInitialized"):
                self._backdrop(window)
        self.changed.emit()


def apply_windows_theme(app):
    available = QStyleFactory.keys()
    preferred = ("windows11", "windowsvista", "Windows", "Fusion") if sys.platform == "win32" else ("Fusion",)
    app.setStyle(next(name for name in preferred if name in available))
    font = QFont()
    # This application is Chinese-first. Segoe's automatic CJK fallback can be
    # SimSun on Windows, so explicitly use the Windows Chinese UI family.
    font.setFamilies(["Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI"])
    font.setPointSizeF(10.5)
    app.setFont(font)
    theme = WindowsTheme(app)
    app.windows_theme = theme
    return theme
