"""Keep Qt lifetime and collection on the GUI thread across desktop tests."""

import gc
import os

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Keep a strong reference for the full run. Individual desktop fixtures may
    # close all their windows without destroying/recreating QApplication.
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def collect_closed_widgets(qt_application):
    yield
    # Process deleteLater after the test's own I/O cleanup, then collect cycles
    # here rather than letting a later I/O thread collect old QWidget wrappers.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
