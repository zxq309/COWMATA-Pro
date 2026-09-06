from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
from cowmata_tailring.workspace.ocr import parse_stamp, valid_roi
from cowmata_tailring.workspace.playback import VideoBoard
from cowmata_tailring.workspace.probe import build_observed_intervals


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def index():
    return MediaTimelineIndex("synthetic", 1, 0, 0, 100, (TimelineSegment(0, 10000, 0, 10000),), ())


def test_failed_last_ocr_remains_browsable_but_not_verified():
    intervals, warnings = build_observed_intervals([
        {"media_ms": 0, "wall_ms": 100000}, {"media_ms": 3000, "wall_ms": 103000},
        {"media_ms": 9900, "wall_ms": None}], index())
    tail = next(i for i in intervals if i["media_end"] == 10000)
    assert tail["wall_end"] == 110000 and not tail["verified"]
    assert warnings


def test_clock_jump_never_silently_bridged():
    intervals, warnings = build_observed_intervals([
        {"media_ms": 0, "wall_ms": 100000}, {"media_ms": 3000, "wall_ms": 203000},
        {"media_ms": 9900, "wall_ms": None}], index())
    assert not intervals and warnings


def test_date_validation_and_four_corner_roi():
    assert parse_stamp("2026年08月03日 星期一 12:44:58") == "2026-08-03 12:44:58"
    assert parse_stamp("2026-02-30 12:44:58") is None
    assert parse_stamp("12:44:58") is None  # Never borrow today's date.
    for roi in ((0, 0, .5, .2), (.5, 0, 1, .2), (0, .8, .5, 1), (.5, .8, 1, 1)):
        assert valid_roi(roi) == roi
    with pytest.raises(ValueError):
        valid_roi((0, 0, 0, 1))


def test_view_relayout_keeps_handles_clock_and_bounded_pool(app):
    board = VideoBoard()
    board.timer.stop()
    board.reference_ms = 100000
    views = [f"camera-{i}" for i in range(8)]
    board.select(views)
    handles = {name: int(tile.surface.winId()) for name, tile in board.tiles.items()}
    board.enlarge(views[-1])
    assert board.reference_ms == 100000
    board.enlarge(None)
    assert handles == {name: int(tile.surface.winId()) for name, tile in board.tiles.items()}
    board.select(views[::-1])
    assert len(board.pool) == 8 and board.reference_ms == 100000
    with pytest.raises(ValueError):
        board.select(views + ["ninth"])
    board.close()


def test_stale_precise_result_cannot_overwrite_latest_request(app):
    board = VideoBoard()
    board.timer.stop()
    tile = board._new_tile()
    tile.asset_id = "same-source"
    tile.pending = {"generation": 2}
    board.generation = 2
    board._precise_ready((1, "same-source", tile, 1000, None, None))
    assert not tile.ready and tile.pending == {"generation": 2}
    tile.asset_id = "replacement-source"
    board._precise_ready((2, "same-source", tile, 1000, None, None))
    assert not tile.ready
    board.close()


def test_only_main_view_has_audio(app):
    board = VideoBoard()
    board.timer.stop()
    board.select(["A", "B", "C"])
    volumes = {}
    for camera, tile in board.tiles.items():
        tile.engine = SimpleNamespace(set_volume=lambda value, c=camera: volumes.update({c: value}), close=lambda: None)
    board.set_main("B")
    assert volumes == {"A": 0, "B": 80, "C": 0}
    board.close()
