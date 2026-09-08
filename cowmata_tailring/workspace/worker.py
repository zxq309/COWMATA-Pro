from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Signal

from cowmata_tailring.media.native_ps import SIGNATURE as NATIVE_SIGNATURE
from cowmata_tailring.media.native_ps import native_hint

from .catalog import assert_not_being_written, file_stamp
from .clocks import ClockMap
from .demand import next_video_task, reference_window
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
        self.job_stop = threading.Event()
        self.wake = threading.Event()
        self.playback_busy = threading.Event()
        self.commands = queue.Queue()
        self.focus_path = None
        self.window = None
        self.budget = 48
        self.hints_used = 0
        self.attempted = set()
        self.explicit = set()
        self.bulk = False
        self.playhead = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="project-index")

    def start(self):
        self.thread.start()

    def request(self, action="scan", value=None):
        if action in {"focus", "window", "pause"}:
            self.job_stop.set()
        self.commands.put((action, value))
        self.wake.set()

    def cancel(self):
        self.stop.set()
        self.job_stop.set()
        self.wake.set()

    def next_task(self, pending):
        by_path = {r["path"]: r for r in pending}
        if self.focus_path in by_path:
            return "full", by_path[self.focus_path]
        for path in sorted(self.explicit):
            if path in by_path:
                return "full", by_path[path]
        if self.window:
            start, end, settings = self.window
            maps = {k: ClockMap.from_dict(v) for k, v in settings.get("camera_maps", {}).items()}
            rows = self.catalog.rows()
            hints = self.catalog.video_hints()
            unavailable = {r["path"] for r in rows if r["state"] in {"pending", "invalid"} and r["path"] not in by_path}
            if self.playhead is not None:
                task = next_video_task(rows, hints, max(start, self.playhead-10000), min(end, self.playhead+60000),
                                       maps=maps, overrides=settings.get("camera_overrides"),
                                       attempted=self.attempted | unavailable, explore=False)
                if task and task[0] == "full" and task[1]["path"] in by_path:
                    return task
            task = next_video_task(rows, hints, start, end,
                                   maps=maps, overrides=settings.get("camera_overrides"), attempted=self.attempted | unavailable,
                                   explore=self.hints_used < self.budget)
            if task and task[0] == "full" and task[1]["path"] in by_path:
                return task
            # Cheap native header discovery precedes OCR exploration. Counters
            # need not be chronological. It does not consume the OCR budget.
            native_candidates = [r for r in pending if r["kind"] == "video" and r["path"] not in hints]
            if native_candidates:
                return "native", native_candidates[0]
            checks = []
            for row in rows:
                if row["metadata"].get("native_check_pending") and row["state"] == "review" and not row["metadata"].get("manual_readings"):
                    lo, hi = reference_window(row, start, end, maps, settings.get("camera_overrides"))
                    spans = row["metadata"].get("intervals", [])
                    if any(s["wall_start"] <= hi and s["wall_end"] >= lo for s in spans):
                        checks.append(row)
            if checks:
                return "verify_native", checks[0]
            if task and task[1]["path"] in by_path:
                return task
        if self.bulk and pending:
            return "full", pending[0]
        return None

    def may_run(self, task):
        if not task:
            return False
        if not self.playback_busy.is_set():
            return True
        # Playback cannot starve the very next clip in the active IMU window.
        # Full-project/exploratory work still yields to the video renderer.
        return bool(self.window and (task[0] in {"full", "native"} or task[1].get("_guided")))

    def run(self):
        if os.name == "nt":
            try:
                kernel = ctypes.WinDLL("kernel32")
                kernel.GetCurrentThread.restype = ctypes.c_void_p
                kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
                kernel.SetThreadPriority(kernel.GetCurrentThread(), 0x10000)
            except (OSError, AttributeError):
                pass
        inspector = SourceInspector(self.catalog.root, self.catalog.meta, self.job_stop, self.progress.emit)
        inspector.defer_native_checks = True
        last_scan = -1e20
        last_hint_notice = -1e20
        audit = False
        idle_reported = False
        try:
            upgraded = self.catalog.queue_ocr_upgrade(TIMESTAMP_SIGNATURE, time_signature=NATIVE_SIGNATURE)
            if upgraded:
                self.progress.emit(f"OCR 算法已升级，{upgraded} 个录像索引等待复核；人工标注保留")
            while not self.stop.is_set():
                while not self.commands.empty():
                    action, value = self.commands.get_nowait()
                    idle_reported = False
                    if action == "focus":
                        self.focus_path, self.window = value, None
                        self.playhead = None
                        self.hints_used, self.attempted = 0, set()
                        self.budget = 48
                    elif action == "window":
                        self.window = value
                        self.playhead = value[2].get("priority_reference_ms", value[0])
                        self.hints_used, self.attempted = 0, set()
                    elif action == "playhead":
                        self.playhead = value
                    elif action == "more":
                        self.budget = self.hints_used + 48
                    elif action == "bulk":
                        self.bulk = bool(value)
                    elif action == "pause":
                        self.bulk, self.window = False, None
                        self.focus_path = None
                        self.explicit.clear()
                    elif action in {"priority", "recheck"}:
                        paths = [value] if action == "recheck" else list(value or [])
                        self.explicit.update(paths)
                        if action == "recheck":
                            self.catalog.recheck(value)
                        last_scan = -1e20
                    elif action == "audit":
                        audit = True
                        last_scan = -1e20
                    else:
                        last_scan = -1e20
                if self.stop.is_set():
                    break
                self.job_stop.clear()
                # A request arriving between queue draining and clearing the
                # old cancel flag must not start another stale OCR operation.
                if not self.commands.empty():
                    continue
                if time.monotonic() - last_scan > 60:
                    result = self.catalog.scan(audit=audit, fast=True)
                    audit = False
                    self.scanned.emit(result)
                    last_scan = time.monotonic()
                # Windows write-handle checks plus before/after identity checks
                # let completed copies start immediately, without a fixed delay.
                task = self.next_task(self.catalog.pending(eager=True))
                if self.may_run(task):
                    mode, row = task
                    path = self.catalog.source_path(row["path"])
                    self.progress.emit(("核验当前所需素材：" if mode == "full" else "快速查找录像时间：") + row["path"])
                    if mode == "verify_native":
                        try:
                            assert_not_being_written(path)
                            metadata = row["metadata"]
                            if file_stamp(path) != row["stamp"]:
                                raise OSError("文件变化，等待刷新")
                            inspector.defer_native_checks = False
                            inspected = inspector.native_video(path, row["asset_id"], metadata["timeline"]["native"],
                                        {"width":metadata.get("width"),"height":metadata.get("height"),"codec_name":metadata.get("codec")},
                                        {"format":{"format_name":metadata.get("format"),"duration":metadata.get("header_duration")}})
                            if not self.job_stop.is_set() and file_stamp(path) == row["stamp"]:
                                # A concurrent manual correction remains authoritative.
                                current = next((r for r in self.catalog.rows() if r["path"] == row["path"]), None)
                                if current and current["asset_id"] == row["asset_id"] and not current["metadata"].get("manual_readings"):
                                    inspected["camera"] = current["metadata"].get("camera", inspected["camera"])
                                    self.catalog.update_metadata(row["asset_id"], inspected)
                                    self.indexed.emit({**row, "metadata":inspected, "state":"review" if inspected["needs_review"] else "ready"})
                        except (OSError, ValueError, RuntimeError) as exc:
                            if not self.job_stop.is_set():
                                self.progress.emit(str(exc))
                                current = next((r for r in self.catalog.rows() if r["path"] == row["path"]), None)
                                if current and current["stamp"] == row["stamp"]:
                                    metadata = dict(current["metadata"])
                                    metadata["native_check_pending"] = False
                                    self.catalog.update_metadata(row["asset_id"], metadata)
                        finally:
                            inspector.defer_native_checks = True
                    elif mode == "full":
                        result = self.catalog.index_one(row["path"], inspector, cancelled=self.job_stop.is_set, eager=True)
                        if not self.job_stop.is_set():
                            self.attempted.add(row["path"])
                            self.explicit.discard(row["path"])
                        if result:
                            motion = getattr(inspector, 'last_motion', None)
                            if motion and motion[:2] == (path.resolve(), result['asset_id']):
                                result = {**result, 'motion':motion[2]}
                            self.indexed.emit(result)
                        else:
                            self.scanned.emit(None)
                    else:
                        try:
                            assert_not_being_written(path)
                            if file_stamp(path) != row["stamp"]:
                                raise OSError("文件变化，等待刷新")
                            hint = ((native_hint(path, timezone_minutes=inspector.timezone_minutes) or {"native_checked": True})
                                    if mode == "native" else inspector.video_hint(path))
                            if not self.job_stop.is_set() and file_stamp(path) == row["stamp"]:
                                self.catalog.save_video_hint(row["path"], row["stamp"], hint)
                        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                            if not self.job_stop.is_set():
                                self.catalog.save_video_hint(row["path"], row["stamp"], {"native_checked": True} if mode == "native" else
                                                             {"start_ms": None, "reason": str(exc), "hint_only": True})
                        if mode != "native" and not self.job_stop.is_set() and not row.get("_guided"):
                            self.hints_used += 1
                        if mode != "native" or time.monotonic()-last_hint_notice >= .5:
                            self.scanned.emit(None)
                            last_hint_notice = time.monotonic()
                    idle_reported = False
                else:
                    if not idle_reported:
                        self.progress.emit("按需待命 · 仅处理当前九轴所需录像；未检索部分不等于无录像")
                        idle_reported = True
                    self.wake.wait(.5)
                    self.wake.clear()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
