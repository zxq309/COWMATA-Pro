"""Ordered lifecycle probes across real pytest fixture teardown boundaries."""
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from cowmata_tailring.workspace.modern_window import MainWindow

# Keep wrappers alive deliberately: teardown must release the native widget
# tree, independently of Python's nondeterministic cyclic garbage collection.
_windows = {}


def test_completed_close_is_requested_for_next_test():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    _windows["closed"] = window
    window.close()
    app.processEvents()
    assert window._closing_requested and window._closed


def test_previous_completed_close_released_native_widgets():
    window = _windows.pop("closed")
    assert not isValid(window), "Closed test windows must not leave native Qt trees in later tests"


def test_cancelled_close_is_left_open_for_next_test():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    _windows["cancelled"] = window
    window._close_choice = "cancel"
    window.close()
    app.processEvents()
    assert not window._closing_requested and not window._closed


def test_previous_cancelled_close_remains_usable():
    window = _windows.pop("cancelled")
    assert isValid(window)
    assert not window._closed
    window.records.addItem("still usable after cancelled close")
    assert window.records.count() == 1
    window._close_choice = "save"
    window.close()
