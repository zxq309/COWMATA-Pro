from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
from cowmata_tailring.workspace.ocr import parse_stamp, valid_roi
from cowmata_tailring.workspace.playback import VideoBoard, WorkspaceEngine
from cowmata_tailring.workspace.probe import build_observed_intervals


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("software", [False, True])
@pytest.mark.parametrize("no_audio", [False, True])
def test_workspace_renderer_preserves_decode_and_audio_choices(monkeypatch, tmp_path, software, no_audio):
    from cowmata_tailring.media.stable_engine import StableMediaEngine

    options = []
    def capture(self, widget, **kwargs):
        options.extend(kwargs["instance_options"])
        raise RuntimeError("captured before native initialization")
    monkeypatch.setattr(StableMediaEngine, "__init__", capture)
    with pytest.raises(RuntimeError, match="captured before"):
        WorkspaceEngine(None, metadata=None, cache=tmp_path, software=software, no_audio=no_audio)
    assert "--vout=direct3d9" in options
    assert "--avcodec-threads=2" in options
    assert ("--avcodec-hw=none" in options) == software
    assert ("--no-audio" in options) == no_audio


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


def test_indexed_seek_resets_counter_but_never_accepts_pre_seek_frame(app):
    board = VideoBoard()
    board.timer.stop()
    board.select(["A"])
    board.playing = True
    tile = board.tiles["A"]
    tile.asset_id = "sample"
    tile.interval = SimpleNamespace(wall_at=lambda ms:ms)
    tile.engine = SimpleNamespace(
        get_time_ms=lambda:1000, stats=lambda:SimpleNamespace(displayed_pictures=100,decoded_video=100),
        video_output_count=lambda:1, is_seekable=lambda:True, pause=lambda _:None,
        set_time_ms=lambda _:None, set_rate=lambda _:None, close=lambda:None,
        _force_avformat=True, _dahua_duration_index=True)
    tile.pending = {"phase":"priming", "generation":board.generation, "asset":"sample",
                    "target":1000, "start":1, "baseline":99, "cold":False}
    board._observe(tile, 2)
    assert tile.pending["phase"] == "seeking" and tile.pending["baseline"] == 0
    assert not tile.ready  # The old 100-picture snapshot cannot certify this seek.
    board.close()
