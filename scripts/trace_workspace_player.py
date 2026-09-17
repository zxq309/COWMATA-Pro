"""Native video integration harness, not simulated playback."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import VideoTimeline, intervals_from_rows, wall_ms
from cowmata_tailring.workspace.playback import VideoBoard, WorkspaceEngine


parser = argparse.ArgumentParser()
parser.add_argument("project")
parser.add_argument("--time", default="2026-08-03 11:40:00")
parser.add_argument("--seconds", type=int, default=20)
parser.add_argument("--play", action="store_true")
parser.add_argument("--no-audio", action="store_true")
parser.add_argument("--software", action="store_true")
parser.add_argument("--compat", action="store_true")
args = parser.parse_args()
app = QApplication([])
catalog = Catalog(args.project)
board = VideoBoard(engine_factory=lambda widget, **kw: WorkspaceEngine(widget, **kw, no_audio=args.no_audio))
board.software_decode = args.software
board.compatibility = args.compat
rows = catalog.rows(kind="video")
timeline = VideoTimeline(intervals_from_rows(rows))
board.configure(catalog, rows, timeline)
board.resize(1200, 600)
board.show()
board.select(timeline.cameras[:2])
board.seek(wall_ms(args.time))
if args.play:
    board.play(True)
count = 0


def sample():
    global count
    count += 1
    for camera, tile in board.tiles.items():
        engine = tile.engine
        stats = engine.stats() if engine else None
        print(json.dumps({"second": count, "camera": camera, "status": engine.current_status if engine else None,
                          "public_ms": engine.get_time_ms() if engine else None,
                          "raw_ms": engine._lib.libvlc_media_player_get_time(engine._player) if engine else None,
                          "pictures": stats.displayed_pictures if stats else None,
                          "decoded": stats.decoded_video if stats else None,
                          "vout": engine.video_output_count() if engine else None,
                          "seekable": engine.is_seekable() if engine else None,
                          "pending": tile.pending, "ready": tile.ready, "message": tile.message.text()}, ensure_ascii=True), flush=True)
    if count >= args.seconds:
        board.close()
        catalog.close()
        app.quit()


timer = QTimer()
timer.timeout.connect(sample)
timer.start(1000)
raise SystemExit(app.exec())
