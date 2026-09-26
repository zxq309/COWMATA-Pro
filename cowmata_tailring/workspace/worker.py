from __future__ import annotations

import copy
import ctypes
import os
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

from cowmata_tailring.media.native_ps import SIGNATURE as NATIVE_SIGNATURE
from cowmata_tailring.media.native_ps import native_hint

from .catalog import assert_not_being_written, file_stamp
from .clocks import ClockMap
from .demand import camera_folder, next_video_task, reference_window
from .probe import SourceInspector
from .rapid_backend import TIMESTAMP_SIGNATURE


def _index_workers():
    """Return the bounded full-index worker count.

    Full indexing is an independent read/inspect pipeline.  Keeping its
    default at sixteen gives each selected view a real lane while the
    in-flight cap below prevents an unbounded future queue from making every
    remaining file look as if it were actively being processed.
    """
    try:
        value = int(os.environ.get("COWMATA_INDEX_WORKERS", "16"))
    except (TypeError, ValueError):
        value = 16
    return max(1, min(16, value))


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
        self._demand_lock = threading.Lock()
        self._requested_demand = None
        self.focus_path = None
        self.window = None
        self.budget = 48
        self.hints_used = 0
        self.attempted = set()
        self.attempted_stamps = {}
        self.explicit = set()
        self.bulk = False
        self.inspect_pool = ThreadPoolExecutor(max_workers=_index_workers(), thread_name_prefix="video-inspect")
        self.max_inflight = _index_workers()
        self._pool_shutdown = False
        self.inspect_pending = {}
        self._thread_state = threading.local()
        self.playhead = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="project-index")

    def start(self):
        self.thread.start()

    def request(self, action="scan", value=None):
        if action in {"focus", "window", "pause"}:
            # A save/position refresh is not a new search. Restarting OCR here
            # discards its progress, leaves the row pending and loops at 1/N.
            # Compare only routing inputs, excluding annotation/UI settings.
            key = (action, value)
            if action == "window":
                start, end, settings = value
                key = (action, start, end, copy.deepcopy(settings.get("camera_maps", {})),
                       copy.deepcopy(settings.get("camera_overrides", {})))
            with self._demand_lock:
                if key == self._requested_demand:
                    if action != "window":
                        return
                    action, value = "playhead", value[2].get("priority_reference_ms", value[0])
                else:
                    self._requested_demand = key
                    self.job_stop.set()
        self.commands.put((action, value))
        self.wake.set()

    def cancel(self):
        self.stop.set()
        self.job_stop.set()
        self.wake.set()

    def _ensure_pool(self):
        # A paused/closed worker shuts the pool down; a restarted worker
        # recreates it instead of crashing on submit.
        if getattr(self, "_pool_shutdown", False):
            self.inspect_pool = ThreadPoolExecutor(
                max_workers=self.max_inflight, thread_name_prefix="video-inspect")
            self._pool_shutdown = False
        return self.inspect_pool

    def _thread_inspector(self):
        """One SourceInspector per working thread; OCR models are not shared."""
        inspector = getattr(self._thread_state, "inspector", None)
        if inspector is None:
            inspector = SourceInspector(self.catalog.root, self.catalog.meta,
                                        self.job_stop, self.progress.emit)
            inspector.defer_native_checks = True
            self._thread_state.inspector = inspector
        return inspector

    def _inspect_full(self, row, path):
        inspector = self._thread_inspector()
        if row["kind"] == "video" and row["metadata"].get("recheck") and not row["metadata"].get("manual_readings"):
            try:
                assert_not_being_written(path)
                inspector.video_hint(path)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                if not self.job_stop.is_set():
                    self.progress.emit(str(exc))
        result = self.catalog.index_one(row["path"], inspector,
                                        cancelled=self.job_stop.is_set, eager=True)
        return result, inspector

    def _harvest_inspections(self):
        """Emit finished parallel inspections; failures stay pending for retry."""
        for future in [f for f in list(self.inspect_pending) if f.done()]:
            row, path = self.inspect_pending.pop(future)
            try:
                result, inspector = future.result()
            except Exception:
                if not self.stop.is_set():
                    self.scanned.emit(None)
                continue
            if self.job_stop.is_set() or self.stop.is_set():
                continue
            if result:
                motion = getattr(inspector, 'last_motion', None)
                if motion and motion[:2] == (path.resolve(), result['asset_id']):
                    result = {**result, 'motion': motion[2]}
                self.indexed.emit(result)
            else:
                self.scanned.emit(None)

    def next_task(self, pending):
        by_path = {r["path"]: r for r in pending}
        for path, stamp in list(self.attempted_stamps.items()):
            if path in by_path and by_path[path]["stamp"] != stamp:
                self.attempted.discard(path)
                del self.attempted_stamps[path]
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
            if task and task[0] == "full" and not task[1].get("_exploratory") and task[1]["path"] in by_path:
                return task
            # Use the same sparse, batch-aware routing for native headers.
            # Scanning every card first delays other cameras in large projects.
            covered_folders = set()
            for row in rows:
                if row["kind"] != "video" or row["state"] not in {"ready", "review"}:
                    continue
                lo, hi = reference_window(row, start if self.playhead is None else self.playhead,
                                          end if self.playhead is None else self.playhead,
                                          maps, settings.get("camera_overrides"))
                if any(s["wall_start"] <= hi and s["wall_end"] > lo for s in row["metadata"].get("intervals", [])):
                    covered_folders.add(camera_folder(row["path"]))
            deferred, skipped = None, set()
            # A deferred camera must not head-of-line block a missing one.
            # Bound lookahead to one discovery batch; skipped rows are NOT
            # marked attempted and remain available when playback pauses.
            for _ in range(48):
                if not task or task[1]["path"] not in by_path:
                    break
                if task[1]["path"] not in hints:
                    task = ("native", task[1])
                elif task[0] == "hint" and camera_folder(task[1]["path"]) not in covered_folders:
                    task = (task[0], {**task[1], "_required_view": True})
                if self.may_run(task):
                    return task
                deferred = deferred or task
                skipped.add(task[1]["path"])
                task = next_video_task(rows, hints, start, end,
                                       maps=maps, overrides=settings.get("camera_overrides"),
                                       attempted=self.attempted | unavailable | skipped,
                                       explore=self.hints_used < self.budget)
            if deferred:
                return deferred
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
        return bool(self.window and not task[1].get("_exploratory") and
                    (task[0] in {"full", "native"} or task[1].get("_guided") or task[1].get("_required_view")))

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
            upgraded_dahua = self.catalog.queue_dahua_timeline_upgrade()
            if upgraded_dahua:
                self.progress.emit(f"大华时间轴规则已升级，{upgraded_dahua} 个原始码流录像等待重建索引；人工标注保留")
            while not self.stop.is_set():
                while not self.commands.empty():
                    action, value = self.commands.get_nowait()
                    idle_reported = False
                    if action == "focus":
                        self.focus_path, self.window = value, None
                        self.playhead = None
                        self.hints_used, self.attempted = 0, set()
                        self.attempted_stamps.clear()
                        self.budget = 48
                    elif action == "window":
                        self.window = value
                        self.playhead = value[2].get("priority_reference_ms", value[0])
                        self.hints_used, self.attempted = 0, set()
                        self.attempted_stamps.clear()
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
                    self.progress.emit("正在清点工程目录（不读取录像正文）…")
                    result = self.catalog.scan(audit=audit, fast=True, cancelled=self.stop.is_set,
                                               progress=self.progress.emit)
                    if self.stop.is_set():
                        break
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
                    elif mode == "full" and row["kind"] == "video" and (self.bulk or self.window is not None):
                        # Full indexing and the active annotation window both
                        # use independent read lanes.  The previous window
                        # path fell through to the serial branch, so selecting
                        # sixteen cameras still left fifteen valid sources in
                        # the waiting state.  Each thread keeps its own
                        # SourceInspector while the catalog serializes commits.
                        # Keep only one bounded batch in flight.  Previously
                        # every discovered file was submitted immediately;
                        # thousands of rows then sat in a futures queue while
                        # the UI reported them as waiting, even though only a
                        # few readers could run.
                        if len(self.inspect_pending) >= self.max_inflight:
                            self._harvest_inspections()
                            if len(self.inspect_pending) >= self.max_inflight:
                                self.wake.wait(.05)
                                self.wake.clear()
                                continue
                        self.attempted.add(row["path"])
                        self.attempted_stamps[row["path"]] = row["stamp"]
                        self.explicit.discard(row["path"])
                        self.inspect_pending[self._ensure_pool().submit(
                            self._inspect_full, row, path)] = (row, path)
                        idle_reported = False
                        self._harvest_inspections()
                    elif mode == "full":
                        if row["kind"] == "video" and row["metadata"].get("recheck") and not row["metadata"].get("manual_readings"):
                            # Legacy intervals can route straight to full OCR.
                            # Prepare the same bounded later-frame cache used
                            # by new imports, so blank openings are not retried
                            # repeatedly before reaching a readable clock.
                            try:
                                assert_not_being_written(path)
                                inspector.video_hint(path)
                            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                                if not self.job_stop.is_set():
                                    self.progress.emit(str(exc))
                        result = self.catalog.index_one(row["path"], inspector, cancelled=self.job_stop.is_set, eager=True)
                        if not self.job_stop.is_set():
                            self.attempted.add(row["path"])
                            self.attempted_stamps[row["path"]] = row["stamp"]
                            self.explicit.discard(row["path"])
                            if row.get("_exploratory"):
                                self.hints_used += 1
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
                            hint = ((native_hint(path, timezone_minutes=inspector.timezone_minutes, cancelled=self.job_stop.is_set) or {"native_checked": True})
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
            if not self.stop.is_set():
                self.failed.emit(str(exc))
        finally:
            deadline = time.monotonic() + 60
            while self.inspect_pending and not self.stop.is_set():
                self._harvest_inspections()
                if not self.inspect_pending or time.monotonic() > deadline:
                    break
                time.sleep(.05)
            self._pool_shutdown = True
            self.inspect_pool.shutdown(wait=False)
            self.finished.emit()
