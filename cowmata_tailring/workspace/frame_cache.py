"""Bounded RAM cache of original decoded frames, never approximate previews."""
import ctypes
import threading
from collections import OrderedDict


def recommended_budget():
    # At most 1/64 of physical RAM and never more than 512 MiB per viewer.
    total = 8 * 1024 ** 3
    try:
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        value = Memory()
        value.length = ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            total = value.total
    except (AttributeError, OSError):
        pass
    return max(32 * 1024 ** 2, min(512 * 1024 ** 2, total // 64))


class FrameCache:
    def __init__(self, max_bytes=None):
        self.max_bytes = recommended_budget() if max_bytes is None else max_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key not in self.entries:
                self.misses += 1
                return None
            self.hits += 1
            self.entries.move_to_end(key)
            actual, image, _ = self.entries[key]
            return actual, image

    def put(self, key, actual, image):
        size = image.sizeInBytes()
        if size > self.max_bytes:
            return
        with self.lock:
            old = self.entries.pop(key, None)
            if old:
                self.bytes -= old[2]
            while self.entries and self.bytes + size > self.max_bytes:
                _, item = self.entries.popitem(last=False)
                self.bytes -= item[2]
            self.entries[key] = actual, image, size
            self.bytes += size

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0

    def stats(self):
        with self.lock:
            return {"hits": self.hits, "misses": self.misses, "bytes": self.bytes,
                    "max_bytes": self.max_bytes, "frames": len(self.entries)}
