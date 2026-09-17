"""Repeatable HD decode/UI stress gate, never modifies original recordings.

Eight unique remuxed test views from real 2560x1440 HEVC footage. Artificial
reference clocks are TEST ONLY, not a claimed field synchronization.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cowmata_tailring.annotation.data import _synthetic_object
from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg, probe_media
from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.media.timeline import probe_media_timeline
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, wall_ms
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def make_fixture(source, root):
    root.mkdir(parents=True, exist_ok=False)
    ffmpeg, ffprobe = find_ffmpeg()
    base = wall_ms("2026-08-03 12:00:00")
    original = digest_file(source)
    prepared, hashes = {}, {}
    common = None
    for view in range(8):
        for part in range(2):
            path = root / f"CAM{view + 1:02d}" / f"{part + 1:03d}.mp4"
            path.parent.mkdir(exist_ok=True)
            process = run_cancellable([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", "0", "-i", str(source),
                                       "-t", "30", "-map", "0:v:0", "-an", "-c", "copy", "-metadata",
                                       f"comment=COWMATA-STRESS-ONLY-{view}-{part}", "-movflags", "+faststart", str(path)], timeout=120)
            if process.returncode:
                raise RuntimeError(process.stderr.decode("utf-8", "replace"))
            if common is None:
                common = probe_media_timeline(path, ffprobe)
                info = next(s for s in probe_media(path)["streams"] if s["codec_type"] == "video")
            stat = path.stat()
            timeline = replace(common, source_path=str(path.resolve()), source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
            start = base + part * common.duration_ms
            prepared[str(path.resolve())] = {"camera": f"CAM{view + 1:02d}", "duration_ms": common.duration_ms,
                "format": "mov,mp4,m4a,3gp,3g2,mj2", "timeline": timeline.to_dict(), "needs_review": False,
                # Controlled synthetic reference, never a field calibration.
                # Explicit anchors keep OCR migrations from replacing the test
                # clock with the unrelated timestamp visible in repeated clips.
                "manual_readings": [{"media_ms": 0, "wall_ms": start, "source": "stress_fixture"},
                                    {"media_ms": common.duration_ms, "wall_ms": start + common.duration_ms,
                                     "source": "stress_fixture"}],
                "intervals": [{"media_start": 0, "media_end": common.duration_ms, "wall_start": start,
                               "wall_end": start + common.duration_ms, "verified": True}]}
            hashes[str(path.relative_to(root))] = digest_file(path)
    obj = _synthetic_object(2)
    obj["device"] = "STRESS-ONLY"
    obj["imu"] = base64.b64encode(b"".join(struct.pack("<I9h", 1000 + i, 4096, i % 700, -1024,
                                                   0, 10, 20, 100, 200, 300) for i in range(0, 120001, 20))).decode()
    imu = root / "IMU" / "stress.json"
    atomic_json(imu, obj)
    hashes[str(imu.relative_to(root))] = digest_file(imu)
    catalog = Catalog(root, stability_seconds=0)
    try:
        catalog.scan()
        inspector = SourceInspector(root, catalog.meta)
        for row in catalog.pending(retry_seconds=0):
            result = catalog.index_one(row["path"], lambda p, k, a: prepared[str(p.resolve())] if k == "video" else inspector(p, k, a))
            if row["kind"] == "imu":
                work = SessionWork(result["asset_id"])
                work.project.cow_id = "STRESS-ONLY"
                work.clock = ClockMap([Anchor(0, base), Anchor(120000, base + 120000)])
                work.progress = {"imu_ms": 4000, "reference_ms": base + 4000}
                atomic_json(catalog.work_path(work.asset_id), work.to_dict())
        catalog.save_settings({"fixture": True, "device": "STRESS-ONLY", "current_asset": work.asset_id,
                               "reference_ms": base + 4000, "selected_cameras": [f"CAM{i + 1:02d}" for i in range(8)]})
    finally:
        catalog.close()
    assert digest_file(source) == original
    atomic_json(root / "fixture-evidence.json", {"base": base, "original_sha256": original, "hashes": hashes,
                "duration_ms": common.duration_ms, "video": {k: info.get(k) for k in ("width", "height", "codec_name", "avg_frame_rate")}})


class Memory(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in
        ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
         "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def working_set():
    count = Memory()
    count.cb = ctypes.sizeof(count)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    call = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    call.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
    call(kernel.GetCurrentProcess(), ctypes.byref(count), count.cb)
    return count.WorkingSetSize / 2 ** 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path)
    ap.add_argument("--project", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--policy", choices=("full", "balanced"), default="full")
    ap.add_argument("--seconds", type=float, default=120)
    args = ap.parse_args()
    if args.source:
        make_fixture(args.source.resolve(), args.project.resolve())
    fixture = json.loads((args.project / "fixture-evidence.json").read_text(encoding="utf-8"))
    base, duration = fixture["base"], fixture["duration_ms"]
    # Repeated trials must start at the same test clock, not saved last position.
    catalog = Catalog(args.project, stability_seconds=0)
    try:
        settings = catalog.settings()
        if settings.get("fixture") is not True:
            raise RuntimeError("Stress runner accepts its own fixture only")
        settings["reference_ms"] = base + 4000
        settings["presentation"] = {}
        settings["main_camera"] = "CAM01"
        settings["device_profiles"] = {}
        # This controlled benchmark always exercises eight views. A prior
        # inspector/history trial may have deliberately persisted no views.
        views = [f"CAM{i:02d}" for i in range(1, 9)]
        settings["selected_cameras"] = views
        settings["camera_order"] = views
        settings["device_views"] = {str(settings["device"]): views}
        catalog.save_settings(settings)
        work_path = catalog.work_path(settings["current_asset"])
        saved = json.loads(work_path.read_text(encoding="utf-8"))
        saved["progress"] = {"imu_ms": 4000, "reference_ms": base + 4000}
        atomic_json(work_path, saved)
    finally:
        catalog.close()
    app = QApplication([])
    app.setStyle("Fusion")
    app.setOrganizationName("COWMATA-Validation")
    app.setApplicationName("HD-Stress")
    w = MainWindow()
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    w.resize(1600, 1000)
    w.show()
    w.open_project(args.project)
    w.set_presentation("B", persist=False)
    samples, heartbeat, switches, stalls = [], [], [], []
    started = time.perf_counter()
    playing_at = None
    last_beat = started
    last_action = 0
    cycles = 0
    first_pause_ready = None
    paused_at = None
    seen = {f"CAM{i + 1:02d}": set() for i in range(8)}
    result = {}
    callbacks = []

    def measured(owner, name):
        original = getattr(owner, name)
        def call(*a, **kw):
            before = time.perf_counter()
            try:
                return original(*a, **kw)
            finally:
                duration = (time.perf_counter() - before) * 1000
                if duration > 50:
                    callbacks.append({"elapsed": before - started, "name": name, "ms": duration})
        setattr(owner, name, call)

    for owner, name in ((w, "save_current"), (w.board, "relayout"), (w.board, "_request"),
                        (w.board, "_preview_ready"), (w, "update_coverage")):
        measured(owner, name)

    def beat():
        nonlocal last_beat
        now = time.perf_counter()
        heartbeat.append(now - last_beat)
        if now - last_beat > .15:
            stalls.append({"elapsed": now - started, "ms": (now - last_beat) * 1000})
        last_beat = now

    def finish():
        timer.stop()
        pulse.stop()
        running = [s for s in samples if s["elapsed"] > (playing_at or 0) + 5]
        gaps = np.array(heartbeat[100:] or heartbeat) * 1000
        active = [s["main_ready"] for s in running]
        result.update(policy=args.policy, seconds=args.seconds, codec=fixture["video"], controlled_repeated_real_frames=True,
            actual_native_vlc=True, first_exact_eight_seconds=first_pause_ready, cycles=cycles,
            heartbeat_p95_ms=float(np.percentile(gaps, 95)), heartbeat_max_ms=float(max(gaps)),
            main_ready_fraction=sum(active) / max(1, len(active)),
            ui_rss_max_mb=max(s["rss_mb"] for s in samples),
            process_cpu_seconds=time.process_time(), logical_processors=os.cpu_count(),
            preview_stats=w.board.preview_stats, main_switches=switches,
            stalls=stalls, seek_latencies=w.board.latencies,
            slow_callbacks=callbacks,
            all_views_crossed_files=all(len(ids) >= 2 for ids in seen.values()),
            frame_samples=samples, mica=w.material_result,
            sources_unchanged=all(digest_file(args.project / p) == digest for p, digest in fixture["hashes"].items()))
        # Explicit gates. Do not report a pressure test as passed on clock alone.
        advances = []
        for a, b in zip(running, running[1:]):
            if a["main"] == b["main"] and a["asset"] == b["asset"]:
                advances.append(b["displayed"] > a["displayed"])
        result["main_display_advanced_fraction"] = sum(advances) / max(1, len(advances))
        rates = [(b["displayed"] - a["displayed"]) / (b["elapsed"] - a["elapsed"])
                 for a, b in zip(running, running[1:]) if a["main"] == b["main"] and a["asset"] == b["asset"]
                 and a["main_ready"] and b["main_ready"] and b["displayed"] >= a["displayed"]]
        result["main_display_fps_median"] = float(np.median(rates)) if rates else 0
        evidence = w.board.evidence()
        result["pause_restores_eight_original_pts_frames"] = (len(evidence) == 8 and
            all(e["frame_ready"] and e["frame_source"] == "ffmpeg_pts" for e in evidence))
        result["passed"] = (playing_at is not None and result["heartbeat_p95_ms"] < 150 and
                            result["heartbeat_max_ms"] < 750 and result["main_display_fps_median"] >= 8 and
                            result["main_ready_fraction"] > .85 and result["main_display_advanced_fraction"] > .55 and
                            result["sources_unchanged"] and result["preview_stats"]["max_inflight"] <= 2 and
                            result["all_views_crossed_files"] and cycles >= 1 and result["pause_restores_eight_original_pts_frames"])
        w.board.play(False)
        w.close()
        atomic_json(args.out, result)
        print(json.dumps({k: v for k, v in result.items() if k != "frame_samples"}, ensure_ascii=True), flush=True)
        app.quit()

    def tick():
        nonlocal playing_at, first_pause_ready, last_action, cycles, paused_at
        elapsed = time.perf_counter() - started
        ready = len(w.board.tiles) == 8 and all(t.ready and not t.pending for t in w.board.tiles.values())
        if playing_at is None and w.motion and ready:
            first_pause_ready = elapsed
            w.playback_policy.setCurrentIndex(1 if args.policy == "balanced" else 0)
            w.board.play(True)
            playing_at = elapsed
        if playing_at is not None and paused_at is None:
            if w.board.reference_ms >= base + duration * 2 - 3000:
                w.board.seek(base + 4000)
                cycles += 1
            action = int((elapsed - playing_at) // 20)
            if action > last_action:
                before = time.perf_counter()
                w.board.set_main(w.board.selected[action % 8])
                w.set_presentation("ABC"[action % 3], persist=False)
                switches.append({"at": elapsed, "callback_ms": (time.perf_counter() - before) * 1000})
                last_action = action
        tile = w.board.tiles.get(w.board.main_camera)
        stats = tile.engine.stats() if tile and tile.engine else None
        for camera, t in w.board.tiles.items():
            if t.interval and (t.ready or t.actual_ms is not None):
                seen[camera].add(t.asset_id)
        samples.append({"elapsed": elapsed, "main": w.board.main_camera, "asset": tile.asset_id if tile else None,
            "main_ready": bool(tile and tile.ready and not tile.pending), "reference_ms": w.board.reference_ms,
            "displayed": stats.displayed_pictures if stats else 0, "decoded": stats.decoded_video if stats else 0,
            "lost": stats.lost_pictures if stats else 0, "rss_mb": working_set(),
            "preview_inflight": len(w.board.preview_tasks), "preview_completed": w.board.preview_stats["completed"],
            "engines_actually_playing": sum(bool(t.engine and t.engine.is_playing()) for t in w.board.pool)})
        if playing_at is not None and elapsed - playing_at >= args.seconds and paused_at is None:
            paused_at = elapsed
            w.board.play(False)
            return
        if (paused_at is not None and (ready or elapsed - paused_at > 30)) or elapsed > args.seconds + 100:
            finish()

    pulse = QTimer()
    pulse.timeout.connect(beat)
    pulse.start(20)
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(500)
    app.exec()
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
