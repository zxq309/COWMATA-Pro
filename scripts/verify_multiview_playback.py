"""Real libVLC 1–8 view/rate/boundary matrix with bounded resource telemetry."""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import VideoTimeline, intervals_from_rows, wall_ms
from cowmata_tailring.workspace.playback import VideoBoard
from cowmata_tailring.workspace.storage import atomic_json


def handles():
    value = ctypes.c_ulong()
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
    return value.value


def memory_mb():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in
            ("peak", "working", "pool_peak", "pool", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    method = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    method.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not method(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError()
    return value.working / 1024 ** 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--out", required=True)
    parser.add_argument("--counts", default="1,2,3,4,5,6,7,8")
    parser.add_argument("--rates", default="1,2,4")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    catalog = Catalog(args.project)
    rows = catalog.rows(kind="video")
    timeline = VideoTimeline(intervals_from_rows(rows))
    board = VideoBoard()
    board.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    board.setWindowTitle("COWMATA 后台播放验收（自动测试素材）")
    board.resize(1200, 680)
    board.configure(catalog, rows, timeline)
    board.show()
    base = wall_ms("2026-08-03 12:00:00")
    cases = [(int(count), float(rate)) for _ in range(args.repeat)
             for count in args.counts.split(",") for rate in args.rates.split(",")]
    results = []
    position = -1
    started = 0
    saw = {}
    samples = []
    first_ready = None

    def next_case():
        nonlocal position, started, saw, samples, first_ready
        position += 1
        if position >= len(cases):
            board.close()
            catalog.close()
            app.quit()
            return
        count, rate = cases[position]
        board.play(False)
        board.select(timeline.cameras[:count])
        board.set_rate(rate)
        board.seek(base + 4000)
        board.play(True)
        started = time.perf_counter()
        saw, samples, first_ready = {camera: set() for camera in board.selected}, [], None
        print(json.dumps({"case": position + 1, "count": count, "rate": rate}), flush=True)

    def tick():
        nonlocal first_ready
        if not 0 <= position < len(cases):
            return
        now = time.perf_counter()
        ready = all(tile.ready and tile.pending is None for tile in board.tiles.values())
        if ready and first_ready is None:
            first_ready = now - started
        for camera, tile in board.tiles.items():
            if tile.ready and tile.asset_id:
                saw[camera].add(tile.asset_id)
        samples.append({"elapsed": now - started, "reference_ms": board.reference_ms,
                        "ready": sum(t.ready for t in board.tiles.values()), "handles": handles(), "memory_mb": memory_mb(),
                        "views": [{"camera": c, "ready": t.ready, "status": t.engine.current_status if t.engine else None,
                                   "phase": t.pending.get("phase") if t.pending else None,
                                   "media_ms": t.engine.get_time_ms() if t.engine else None,
                                   "displayed": t.engine.stats().displayed_pictures if t.engine and t.engine.stats() else 0,
                                   "asset": t.asset_id} for c, t in board.tiles.items()]})
        # Require simultaneous actual readiness, not merely a master-clock crossing.
        done = board.reference_ms >= base + 26000 and ready
        timeout = now - started > 65
        if done or timeout:
            count, rate = cases[position]
            result = {"case": position + 1, "count": count, "rate": rate, "seconds": now - started,
                      "first_all_ready_seconds": first_ready, "timed_out": timeout,
                      "all_crossed_to_second_file": all(len(assets) >= 2 for assets in saw.values()),
                      "all_ready_at_end": ready, "player_count": sum(t.engine is not None for t in board.pool),
                      "handles": handles(), "memory_mb": memory_mb(), "sources_seen": {c: sorted(v) for c, v in saw.items()}}
            results.append(result)
            atomic_json(output / f"case-{position + 1:02d}.json", {"summary": result, "samples": samples})
            atomic_json(output / "summary.json", results)
            atomic_json(output / "seek_metrics.json", board.latencies)
            print(json.dumps(result, ensure_ascii=True), flush=True)
            next_case()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(250)
    QTimer.singleShot(0, next_case)
    app.exec()
    return 0 if results and all(not r["timed_out"] and r["all_crossed_to_second_file"] and r["all_ready_at_end"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
