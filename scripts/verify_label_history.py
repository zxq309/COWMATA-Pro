"""Actual native history playback; fixture clocks are TEST ONLY, not cow truth."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.app.resources import prioritize_ui
from cowmata_tailring.workspace.catalog import META_DIR, digest_file
from cowmata_tailring.workspace.history_window import HistoryWindow
from cowmata_tailring.workspace.label_file import build_label_file, read_index, save_label_file
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--policy", choices=["full", "balanced"], default="full")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--reopen", action="store_true")
    parser.add_argument("--event-raw", type=Path, help="Continuously infer this real V2 record during eight-view playback")
    parser.add_argument("--no-frame-cache", action="store_true", help="Controlled comparison only: disable the RAM cache")
    args = parser.parse_args()
    root = args.project.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    rows, settings = read_index(root)
    if settings.get("fixture") is not True:
        raise ValueError("Only an explicitly marked isolated fixture is permitted")
    work = SessionWork.from_dict(json.loads((root / META_DIR / "annotations" / (settings["current_asset"] + ".json")).read_text(encoding="utf-8")))
    row = next(r for r in rows if r["kind"] == "imu" and r["asset_id"] == work.asset_id)
    motion = load_motion_json(root / row["path"])
    # These synthetic annotations test coordinates and MUST NOT enter datasets.
    work.project.events = []
    work.project.add_event(0, 27000, 33000, note="TEST ONLY: boundary crossing, not cow truth")
    doc = build_label_file(work, motion, root, rows, settings, selection=(25000, 45000))
    path = args.out / "single-snippet.annotations.json"
    save_label_file(path, doc)
    hashes = {str(p.relative_to(root)): digest_file(p) for p in root.rglob("*") if p.is_file()}
    label_hash = digest_file(path)
    app = QApplication([])
    scheduling = prioritize_ui()
    app.setStyle("Fusion")
    window = HistoryWindow(path, reusable=args.reopen)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.policy.setCurrentIndex(1 if args.policy == "balanced" else 0)
    window.show()
    began = time.perf_counter()
    last_heartbeat = began
    heartbeats, samples = [], []
    stalls = []
    state = "loading"
    state_at = began
    result = {"fixture_only": True, "policy": args.policy, "phase": "history", "close_reopen": args.reopen, "screenshots": []}
    result["scheduling"] = scheduling
    if args.no_frame_cache:
        window.board.frame_cache.max_bytes = 0
    slow_calls = []
    def measured(owner, name):
        original = getattr(owner, name)
        def call(*values, **kwargs):
            before = time.perf_counter()
            try:
                return original(*values, **kwargs)
            finally:
                duration = (time.perf_counter() - before) * 1000
                if duration > 100:
                    slow_calls.append({"elapsed": before - began, "name": name, "ms": duration})
        setattr(owner, name, call)
    for name in ("_request", "_precise_request", "_pause_tile", "relayout", "set_main", "seek", "play", "_start_queued_decoder"):
        measured(window.board, name)
    cycles = 0
    event_stop = threading.Event()
    event_thread = None
    event_results, event_errors = [], []
    event_hash = digest_file(args.event_raw) if args.event_raw else None

    def event_load():
        from cowmata_tailring.workspace.event_models import available_packs, predict_one
        pack = available_packs()[0]
        raw_motion = load_motion_json(args.event_raw)
        while not event_stop.is_set():
            for model in pack["models"]:
                if event_stop.is_set():
                    return
                try:
                    item = predict_one(pack, model, args.event_raw, event_hash, "STRESS-TEST-ONLY", raw_motion.duration_ms,
                                       args.out / "event_cache", cancelled=event_stop.is_set, force=True)
                    event_results.append({"model": model["id"], "seconds": item["elapsed_s"], "candidates": len(item["candidates"])})
                except Exception as exc:
                    if not event_stop.is_set():
                        event_errors.append(str(exc))
                    return

    def heartbeat():
        nonlocal last_heartbeat
        now = time.perf_counter()
        heartbeats.append((now - last_heartbeat) * 1000)
        if heartbeats[-1] > 150:
            stalls.append({"elapsed": now - began, "gap_ms": heartbeats[-1], "state": state,
                           "reference": window.board.reference_ms, "main": window.board.main_camera})
        last_heartbeat = now

    def finish(error=None):
        timer.stop()
        heartbeat_timer.stop()
        event_stop.set()
        if event_thread is not None:
            event_thread.join(timeout=5)
            result["background_events"] = {"runs": event_results, "errors": event_errors, "stopped": not event_thread.is_alive(),
                                           "raw_unchanged": digest_file(args.event_raw) == event_hash}
        result["error"] = error
        result["effective_policy"] = window.board.playback_policy
        result["original_frame_cache"] = window.board.frame_cache.stats()
        result["slow_calls"] = slow_calls
        result["samples"] = samples
        result["ui_p95_ms"] = float(np.percentile(heartbeats, 95))
        result["ui_max_ms"] = max(heartbeats)
        result["stalls"] = stalls
        result["unchanged_sources_and_work"] = all(digest_file(root / p) == h for p, h in hashes.items())
        result["unchanged_annotation"] = digest_file(path) == label_hash
        result["eight_second_files"] = any(len(s["tiles"]) == 8 and all(t["path"].endswith("002.mp4") for t in s["tiles"].values()) for s in samples)
        rates = []
        for a, b in zip(samples, samples[1:]):
            if not a["playing"] or not b["playing"] or a["main"] != b["main"]:
                continue
            camera = a["main"]
            x, y = a["tiles"].get(camera), b["tiles"].get(camera)
            if x and y and x["ready"] and y["ready"] and x["path"] == y["path"] and y["displayed"] >= x["displayed"]:
                rates.append((y["displayed"] - x["displayed"]) / (b["elapsed"] - a["elapsed"]))
        result["main_display_fps_median"] = float(np.median(rates)) if rates else 0
        result["passed"] = not error and result["unchanged_sources_and_work"] and result["unchanged_annotation"] and result["eight_second_files"] and result["ui_p95_ms"] < 150 and result["ui_max_ms"] < 750 and result["main_display_fps_median"] >= 8
        window.grab().save(str(args.out / "history-final.png"))
        result["pool_size"] = len(window.board.pool)
        result["passed"] &= result["pool_size"] <= 9
        if args.event_raw:
            result["passed"] &= event_thread is not None and not event_errors and not event_thread.is_alive() and len({r["model"] for r in event_results}) == 5 and digest_file(args.event_raw) == event_hash
        window.dispose()
        atomic_json(args.out / "report.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "samples"}), flush=True)
        app.quit()

    def sample():
        nonlocal state, state_at, cycles, event_thread
        now = time.perf_counter()
        if now - began > 100 + 30 * args.cycles:
            finish("Timeout at " + state + ": " + window.banner.text())
            return
        if window.data:
            samples.append({"elapsed": now - began, "reference": window.board.reference_ms, "playing": window.board.playing, "main": window.board.main_camera,
                "tiles": {c: {"path": t.interval.path if t.interval else "", "ready": t.ready, "preview": window.board.is_preview(c),
                                "status": t.message.text(), "precise_ms": t.precise_ms,
                                "displayed": t.engine.stats().displayed_pictures if t.engine and t.engine.stats() else 0} for c, t in window.board.tiles.items()}})
        if state == "loading" and window.data and len(window.board.tiles) == 8 and all(t.ready for t in window.board.tiles.values()):
            if not np.array_equal(window.data.motion.times_ms, motion.times_ms[(motion.times_ms >= 25000) & (motion.times_ms <= 45000)]):
                finish("Snippet sample time mismatch")
                return
            # Screenshot encoding is not an annotation action. Capture after
            # measurement in finish(), so its synchronous PNG write is not
            # mistaken for playback latency.
            state, state_at = "playing", now
            window.toggle_play()
            if args.event_raw and event_thread is None:
                event_thread = threading.Thread(target=event_load, name="stress-event-load", daemon=True)
                event_thread.start()
        elif state == "playing":
            if now - state_at > 12:
                window.view.setCurrentIndex(0)
                window.board.set_main("CAM02")
            elif now - state_at > 6:
                window.view.setCurrentIndex(1)
                window.board.set_main("CAM05")
            if not window.board.playing:
                state, state_at = "paused", now
        elif state == "paused" and now - state_at > 1 and all(t.ready and t.precise_ms is not None for t in window.board.tiles.values()):
            if abs(window.board.reference_ms - work.clock.map(45000)) > 1:
                finish("Did not pause at snippet end")
                return
            for camera, tile in window.board.tiles.items():
                match = window.board.timeline.locate(camera, window.board.reference_ms)
                if match is None or abs(tile.precise_ms - match[1]) > 150:
                    finish("Original paused frame missed the requested PTS")
                    return
            cycles += 1
            result["completed_cycles"] = cycles
            if cycles < args.cycles:
                if args.reopen:
                    window.close()
                    window.begin_load(root)
                    window.show()
                else:
                    window.seek(25000)
                state, state_at = "loading", now
                return
            result["exact_pause_all_eight"] = True
            result["parent_offset_ms"] = doc["embedded_imu"]["parent_start_ms"]
            finish()

    heartbeat_timer = QTimer()
    heartbeat_timer.timeout.connect(heartbeat)
    heartbeat_timer.start(20)
    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(300)
    app.exec()
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
