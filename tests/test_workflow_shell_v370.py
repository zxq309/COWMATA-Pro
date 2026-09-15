import ctypes
import sys

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from cowmata_tailring.workspace.modern_window import MainWindow


def actions(menu):
    result = []
    for action in menu.actions():
        if action.menu():
            result.extend(actions(action.menu()))
        elif not action.isSeparator():
            result.append(action)
    return result


def test_all_operator_workflows_remain_reachable():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        menus = {a.text().split("(")[0]: a.menu() for a in w.menuBar().actions()}
        assert list(menus) == ["文件", "数据准备", "标注与复核", "数据集构建", "健康与繁殖", "帮助"]
        texts = {a.text() for a in actions(w.menuBar())}
        assert {
            "完整成果…",
            "所选片段…",
            "训练数据…",
            "导出标准 MP4 副本…",
            "接收成果…",
            "回传设置…",
            "撤销",
            "重做",
            "自动生成候选…",
        } <= texts
        assert len(w.algorithm_actions) == 21
        reachable = set(actions(w.menuBar()))
        assert all(a in reachable for a in w.algorithm_actions.values())
        assert any(a.objectName() == "edgeDataDownloadAction" for a in actions(menus["数据准备"]))
        assert not any(a.shortcut().toString() == "F" for a in actions(w.menuBar()))
    finally:
        w.close()
        app.processEvents()


@pytest.mark.parametrize("kind", ["classification", "dataset", "download", "records"])
def test_work_windows_minimize_independently(kind, tmp_path):
    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()
    owner.setWindowTitle("COWMATA owner test")
    if kind == "classification":
        from cowmata_tailring.workspace.organization_ui import OrganizationWindow

        window = OrganizationWindow(owner)
    elif kind == "dataset":
        from cowmata_tailring.workspace.dataset_build_ui import DatasetBuildWindow

        window = DatasetBuildWindow(owner)
    elif kind == "download":
        from PySide6.QtCore import QSettings

        from cowmata_tailring.edge_download.dialog import DownloadDialog

        window = DownloadDialog(
            owner, QSettings(str(tmp_path / "download.ini"), QSettings.Format.IniFormat)
        )
    else:
        from cowmata_tailring.workspace.classification_viewer import ClassificationReportWindow

        window = ClassificationReportWindow(owner, lambda: None)
    owner.show()
    window.show()
    app.processEvents()
    try:
        assert window.parentWidget() is None, "Work window is still owned by the main QWidget"
        if sys.platform == "win32" and app.platformName() == "windows":
            user = ctypes.WinDLL("user32")
            user.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user.GetWindow.restype = ctypes.c_void_p
            assert not user.GetWindow(int(window.winId()), 4), (
                "Native owned window has no independent taskbar button"
            )
        window.showMinimized()
        app.processEvents()
        assert window.isMinimized() and not owner.isMinimized()
        window.showNormal()
        app.processEvents()
        assert not window.isMinimized() and window.isVisible()
        assert window in owner._task_windows.values()
    finally:
        window.close()
        owner.close()
        window.deleteLater()
        owner.deleteLater()
        app.processEvents()


def test_playback_settings_allow_main_window_work_to_continue():
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    seen = []

    def inspect():
        seen.append(w.options.isModal())
        w.options.close()

    QTimer.singleShot(20, inspect)
    w.presentation_settings()
    # Slow CI/active disk transfers can delay delivery beyond a fixed 40 ms.
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        QTest.qWait(10)
    try:
        assert seen == [False], "Settings block the annotation workspace when minimized"
    finally:
        w.close()
        app.processEvents()


def test_canceling_child_close_keeps_other_windows_alive():
    from cowmata_tailring.ui.task_window import TaskWindow, close_task_windows

    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()

    class PendingReview(TaskWindow):
        def closeEvent(self, event):
            event.ignore()

    child = PendingReview(owner)
    child.show()
    try:
        assert close_task_windows(owner) is False
        assert child.isVisible()
    finally:
        child.hide()
        child.deleteLater()
        owner.deleteLater()
        app.processEvents()


def test_menu_reopens_the_minimized_existing_task():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.open_organization(1)
    child = w._organization_window
    child.showMinimized()
    app.processEvents()
    w.open_organization(1)
    app.processEvents()
    try:
        assert not child.isMinimized()
        assert w._organization_window is child
    finally:
        child.close()
        w.close()
        app.processEvents()
