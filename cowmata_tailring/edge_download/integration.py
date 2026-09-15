"""Small, idempotent integration surface for current and future annotators."""
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QAction


class DownloadIntegration(QObject):
    def __init__(self, window, tools_menu):
        super().__init__(window)
        self.window, self.dialog = window, None
        self.closing = False
        self.action = QAction('端侧数据下载…', window)
        self.action.setObjectName('edgeDataDownloadAction')
        self.action.triggered.connect(self.open)
        tools_menu.addAction(self.action)
        window.installEventFilter(self)

    def open(self):
        if self.dialog is None:
            from .dialog import DownloadDialog
            self.dialog = DownloadDialog(self.window)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def eventFilter(self, watched, event):
        # Qt may deliver final events while Python attributes are being cleared.
        window = getattr(self, 'window', None)
        dialog = getattr(self, 'dialog', None)
        if watched is window and event.type() == QEvent.Type.Close and dialog and dialog.running:
            # Never destroy a live QThread. Resume the host's normal close flow
            # (including its own unsaved-work prompts) once cancellation finishes.
            event.ignore()
            if not self.closing:
                self.closing = True
                self.dialog.worker.finished.connect(self.resume_close)
            self.dialog.stop_task()
            return True
        if watched is window and event.type() == QEvent.Type.Hide and not window.isVisible() and dialog and not dialog.running:
            dialog.hide()
        return False

    def resume_close(self):
        self.closing = False
        QTimer.singleShot(0, self.window.close)


def install_menu(window, tools_menu):
    controller = getattr(window, '_edge_download_integration', None)
    if controller is None:
        controller = DownloadIntegration(window, tools_menu)
        window._edge_download_integration = controller
    return controller
