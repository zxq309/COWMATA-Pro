"""Reserve only a native surface during paused UI setup, never a decoder."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.catalog import file_stamp
from cowmata_tailring.workspace.clocks import VideoInterval, VideoTimeline
from cowmata_tailring.workspace.playback import VideoSurface
from cowmata_tailring.workspace.presentation import PresentationVideoBoard


@pytest.fixture
def board(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    widget = PresentationVideoBoard()
    for timer in (widget.timer, widget.decoder_timer, widget.control_timer):
        timer.stop()
    widget.catalog = SimpleNamespace(meta=tmp_path, source_path=lambda p: tmp_path / p)
    monkeypatch.setattr(widget, "seek", lambda value: None)
    widget.resize(1100, 600)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


def wait_for_spare(board):
    for _ in range(100):
        if board.prewarm is not None:
            return board.prewarm
        QTest.qWait(10)
    raise AssertionError("paused layout never prepared its spare native surface")


def test_paused_layout_prepares_one_hidden_native_surface_without_decoding(board, monkeypatch):
    native_calls = []
    original = VideoSurface.winId

    def observe(surface):
        native_calls.append((surface, board.playing))
        return original(surface)

    monkeypatch.setattr(VideoSurface, "winId", observe)
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    assert board.prewarm is None and len(board.pool) == 8  # Work is deferred.
    spare = wait_for_spare(board)
    assert len(board.pool) == 9
    assert spare.isHidden() and spare not in board.tiles.values()
    assert spare.surface.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    assert native_calls == [(spare.surface, False)]
    assert all(tile.engine is None for tile in board.pool)
    assert spare.interval is None and spare.asset_id is None


def test_latest_selection_and_layout_keep_only_one_reusable_spare(board):
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    board.select([f"CAM{i:02d}" for i in range(2, 10)])
    board.set_presentation("C")
    board.set_main("CAM05")
    spare = wait_for_spare(board)
    handle = int(spare.surface.winId())
    for start in (1, 4, 2):
        board.select([f"CAM{i:02d}" for i in range(start, start + 8)])
        board.set_presentation("B")
        QTest.qWait(60)
        assert board.prewarm is spare
        assert int(spare.surface.winId()) == handle
        assert spare not in board.tiles.values() and spare.isHidden()
        assert len(board.pool) == 9
    assert all(tile.engine is None for tile in board.pool)


def test_close_discards_queued_surface_preparation(board):
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    board.close()
    QTest.qWait(100)
    assert board.prewarm is None and len(board.pool) == 8


def test_play_started_before_callback_skips_preparation_until_next_pause(board):
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    board.playing = True
    QTest.qWait(120)
    assert board.prewarm is None and len(board.pool) == 8
    board.playing = False
    board.relayout()
    assert wait_for_spare(board).engine is None


def test_playback_prewarm_reuses_native_handle_prepared_while_paused(board, tmp_path):
    constructions = []

    class Engine(QObject):
        error_occurred = Signal(str)

        def __init__(self, surface, **kwargs):
            super().__init__(kwargs["parent"])
            constructions.append((board.playing, surface.testAttribute(Qt.WidgetAttribute.WA_WState_Created), int(surface.winId())))
            self._path = ""
            self.current_status = "ready"

        def open(self, path):
            self._path = str(path)
            return True

        def set_volume(self, value):
            pass

        def set_rate(self, value):
            pass

        def stats(self):
            return None

        def play(self):
            return True

        def close(self):
            pass

    board.engine_factory = Engine
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    spare = wait_for_spare(board)
    handle = int(spare.surface.winId())
    assert not constructions
    path = tmp_path / "002.mp4"
    path.write_bytes(b"isolated decoder lifecycle fixture")
    first = VideoInterval("first", "001.mp4", "CAM01", 0, 3000, 0, 3000)
    second = VideoInterval("second", "002.mp4", "CAM01", 3000, 6000, 0, 3000)
    board.timeline = VideoTimeline([first, second])
    board.source_stamps["002.mp4"] = file_stamp(path)
    board.tiles["CAM01"].interval = first
    board.tiles["CAM01"].asset_id = "first"
    board.tiles["CAM01"].ready = True
    board.reference_ms = 2000
    board.playing = True
    board._prepare_next()
    assert constructions == [(True, True, handle)]
    assert board.prewarm is spare and spare.asset_id == "second"
    assert len(board.pool) == 9 and board.reference_ms == 2000
