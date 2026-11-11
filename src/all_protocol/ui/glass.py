"""Optional documented Windows 11 Desktop Acrylic; never blur widget text."""
import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def acrylic_supported():
    return (sys.platform == "win32" and sys.getwindowsversion().build >= 22621
            and QApplication.platformName() == "windows")


def apply_backdrop(window, *, enabled, dark):
    supported = acrylic_supported()
    # WA_TranslucentBackground also enables WA_NoSystemBackground. With our
    # native framed windows that leaves old backing-store pixels behind when
    # transparent children (e.g. QLabel) repaint. Keep Qt's background clearing
    # active, including when disabling Acrylic or falling back after failure.
    window.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, enabled and supported)
    window.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    window.setProperty("acrylic", False)
    if not supported:
        return False
    try:
        dwm = ctypes.WinDLL("dwmapi")
        set_attribute = dwm.DwmSetWindowAttribute
        set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        set_attribute.restype = ctypes.c_long
        extend = dwm.DwmExtendFrameIntoClientArea
        extend.argtypes = (wintypes.HWND, ctypes.c_void_p)
        extend.restype = ctypes.c_long
        # Preserve the OS title bar, resize borders, Snap, and caption buttons.
        hwnd = wintypes.HWND(int(window.winId()))
        theme = ctypes.c_int(int(dark))
        set_attribute(hwnd, 20, ctypes.byref(theme), ctypes.sizeof(theme))
        material = ctypes.c_int(3 if enabled else 1)  # DWMSBT_TRANSIENTWINDOW / NONE
        result = set_attribute(hwnd, 38, ctypes.byref(material), ctypes.sizeof(material))
        margins = (ctypes.c_int * 4)(*([-1] * 4 if enabled else [0] * 4))
        extended = extend(hwnd, ctypes.byref(margins))
        success = result == 0 and extended == 0
        if not success:
            window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        window.setProperty("acrylic", enabled and success)
        return enabled and success
    except (OSError, AttributeError):
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        return False
