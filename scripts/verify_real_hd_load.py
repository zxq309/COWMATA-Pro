"""Eight independent decoders of a supplied HD recording: a load test, not eight physical cameras."""
import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import VideoTimeline, intervals_from_rows, wall_ms
from cowmata_tailring.workspace.presentation import PresentationVideoBoard
from cowmata_tailring.workspace.storage import atomic_json
from scripts.verify_multiview_playback import handles, memory_mb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--policy", choices=("full", "balanced"), default="full")
    args = parser.parse_args()
    app = QApplication([])
    catalog = Catalog(args.project)
    rows = catalog.rows(kind="video")
    hd = [r for r in rows if r["metadata"].get("width", 0) >= 2500]
    intervals = intervals_from_rows(hd)
    cloned = [replace(interval, camera=f"HD decoder {i + 1}") for i in range(8) for interval in intervals]
    board = PresentationVideoBoard()
    board.set_presentation("B")
    board.set_policy(args.policy)
    board.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    board.setWindowTitle("COWMATA eight-HD decoder load test - repeated source")
    board.resize(1300, 800)
    board.configure(catalog, rows, VideoTimeline(cloned))
    board.compatibility = True
    board.select(board.timeline.cameras)
    board.show()
    board.seek(wall_ms("2026-08-03 11:40:00"))
    board.play(True)
    started = time.monotonic()
    samples = []
    def tick():
        elapsed = time.monotonic() - started
        samples.append({"seconds": elapsed, "ready": sum(t.ready and not t.pending for t in board.tiles.values()),
                        "reference_ms": board.reference_ms, "memory_mb": memory_mb(), "handles": handles(),
                        "views": [{"ready": t.ready, "actual_ms": t.actual_ms,
                                   "phase": t.pending.get("phase") if t.pending else None,
                                   "displayed": t.engine.stats().displayed_pictures if t.engine and t.engine.stats() else 0} for t in board.tiles.values()]})
        print(json.dumps({k:v for k,v in samples[-1].items() if k != "views"}), flush=True)
        if elapsed >= args.seconds:
            atomic_json(Path(args.out), {"source_is_repeated_for_load_only": True, "rate": 1, "count": 8, "policy": args.policy,
                                        "dimensions": [[r["metadata"].get("width"), r["metadata"].get("height")] for r in hd],
                                        "samples": samples, "latencies": board.latencies})
            board.close()
            catalog.close()
            app.quit()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(1000)
    app.exec()


if __name__ == "__main__":
    main()
