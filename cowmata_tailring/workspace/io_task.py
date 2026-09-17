"""Bounded disk work with a responsive, modal owner and no Qt in the worker."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout


class _Progress(QDialog):
    def __init__(self, parent, message):
        super().__init__(parent)
        self.completed = False
        self.setWindowTitle("正在处理，请稍候")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)

    def reject(self):
        if self.completed:
            super().reject()

    def closeEvent(self, event):
        if self.completed:
            super().closeEvent(event)
        else:
            event.ignore()


def run_io_task(parent, message, operation):
    """Return only after completion; caller keeps its catalog/lease and close guard."""
    dialog = _Progress(parent, message)
    timer = QTimer(dialog)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-io") as pool:
        future = pool.submit(operation)
        def poll():
            if future.done():
                dialog.completed = True
                dialog.accept()
        timer.timeout.connect(poll)
        timer.start(20)
        try:
            while not future.done():
                dialog.exec()
            return future.result()
        finally:
            timer.stop()
            dialog.completed = True
            dialog.close()
            dialog.deleteLater()
