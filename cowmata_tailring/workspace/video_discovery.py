"""Cancellable, coalesced directory discovery; never reads video payloads."""

import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import organization as core
from .video_intake import protected_file

ALIASES = {
    "乐橙": "视角01",
    "右1": "视角02",
    "右2": "视角03",
    "右3": "视角04",
    "左1": "视角05",
    "左2": "视角06",
    "左3": "视角07",
}
ALIASES.update({f"视角{i:02}": f"视角{i:02}" for i in range(1, 21)})


def discover(root, cancelled, publish):
    root = core.safe_path(root)
    found = {}
    last = 0

    def notify(done=False, error=""):
        nonlocal last
        now = time.monotonic()
        if done or now - last >= 0.15:
            publish(
                dict(
                    root=str(root),
                    counts={str(k): v for k, v in found.items()},
                    done=done,
                    error=error,
                )
            )
            last = now

    try:
        # Known view folders can be shown before counting recordings below them.
        for folder in root.iterdir():
            core.check_cancel(cancelled)
            if folder.name in ALIASES and folder.is_dir():
                core.safe_path(folder)
                found[folder] = 0
        notify()
        for path in core.walk_files(root, cancelled):
            if path.suffix.lower() not in core.VIDEO_SUFFIXES or protected_file(path):
                continue
            parents = (p for p in path.parents if p == root or p.is_relative_to(root))
            folder = next((p for p in parents if p.name in ALIASES), None)
            if folder is None:
                parts = path.relative_to(root).parts
                folder = root / parts[0] if len(parts) > 1 else root
            found[folder] = found.get(folder, 0) + 1
            notify()
        found = {p: n for p, n in found.items() if n}
        notify(True)
    except InterruptedError:
        pass
    except (OSError, ValueError) as exc:
        notify(True, str(exc))


class VideoDirectoryScanner(QObject):
    updated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop = threading.Event()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)
        self.mail = None

    def start(self, root):
        self.cancel()
        stop = threading.Event()
        mail = queue.Queue(maxsize=1)
        self.stop = stop
        self.mail = mail

        def publish(value):
            if stop.is_set():
                return
            try:
                mail.get_nowait()
            except queue.Empty:
                pass
            try:
                mail.put_nowait(value)
            except queue.Full:
                pass

        def run():
            try:
                discover(root, stop.is_set, publish)
            except (OSError, ValueError) as exc:
                publish(dict(root=str(root), counts={}, done=True, error=str(exc)))

        threading.Thread(target=run, daemon=True, name="video-directory-discovery").start()
        self.timer.start()

    def poll(self):
        try:
            value = self.mail.get_nowait()
        except (queue.Empty, AttributeError):
            return
        if value["done"]:
            self.timer.stop()
        self.updated.emit(value)

    def cancel(self):
        self.stop.set()
        self.timer.stop()
        self.mail = None
