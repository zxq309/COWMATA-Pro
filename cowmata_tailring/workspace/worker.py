from __future__ import annotations

import queue
import threading
import time

from PySide6.QtCore import QObject, Signal

from .probe import SourceInspector
from .rapid_backend import TIMESTAMP_SIGNATURE


class IndexWorker(QObject):
    scanned = Signal(object)
    indexed = Signal(object)
    progress = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.playback_busy = threading.Event()
        self.commands = queue.Queue()
        self.preferred_paths = []
        self.thread = threading.Thread(target=self.run, daemon=True, name="project-index")

    def start(self):
        self.thread.start()

    def request(self, action="scan", value=None):
        self.commands.put((action, value))
        self.wake.set()

    def cancel(self):
        self.stop.set()
        self.wake.set()

    def run(self):
        inspector = SourceInspector(self.catalog.root, self.catalog.meta, self.stop, self.progress.emit)
        last_scan = -1e20
        audit = False
        try:
            upgraded = self.catalog.queue_ocr_upgrade(TIMESTAMP_SIGNATURE)
            if upgraded:
                self.progress.emit(f"OCR 算法已升级，{upgraded} 个录像索引等待复核；人工标注保留")
            while not self.stop.is_set():
                while not self.commands.empty():
                    action, value = self.commands.get_nowait()
                    if action == "priority":
                        self.preferred_paths = list(value or [])
                    elif action == "recheck":
                        self.catalog.recheck(value)
                        last_scan = -1e20
                    elif action == "audit":
                        audit = True
                        last_scan = -1e20
                    else:
                        last_scan = -1e20
                if time.monotonic() - last_scan > 20:
                    result = self.catalog.scan(audit=audit)
                    audit = False
                    self.scanned.emit(result)
                    last_scan = time.monotonic()
                # Already indexed materials remain usable while bulk OCR yields.
                pending = self.catalog.pending()
                pending.sort(key=lambda r: (r["path"] not in self.preferred_paths, r["kind"] != "imu", r["path"]))
                if pending and not self.playback_busy.is_set():
                    row = pending[0]
                    self.progress.emit("建立索引：" + row["path"])
                    result = self.catalog.index_one(row["path"], inspector)
                    if result:
                        self.indexed.emit(result)
                    else:
                        self.scanned.emit(None)
                else:
                    self.wake.wait(1)
                    self.wake.clear()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
