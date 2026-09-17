"""Player behavior when a derived compatibility file cannot be built."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
from cowmata_tailring.workspace.catalog import Catalog, file_stamp
from cowmata_tailring.workspace.clocks import VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.playback import VideoBoard


class RendererBoundary(QObject):
    """Native rendering is external; source selection and readiness stay real."""
    error_occurred = Signal(str)

    def __init__(self, _surface, *, parent=None, **_kwargs):
        super().__init__(parent)
        self._path = ""
        self.current_status = "opening"
        self.last_error = ""

    def open(self, path):
        self._path = str(path)
        return True

    def pause(self, _value):
        pass

    def play(self):
        pass

    def clear_pending_seek(self):
        pass

    def set_volume(self, _value):
        pass

    def set_rate(self, _value):
        pass

    def stats(self):
        return SimpleNamespace(displayed_pictures=0)

    def get_time_ms(self):
        return 0

    def close(self):
        pass


@pytest.fixture
def player(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"PS source identity; decoding covered by real native tests")
    row = {"state": "ready", "asset_id": "a" * 64, "path": source.name,
           "stamp": file_stamp(source), "metadata": {"camera": "A", "format": "mpeg",
            "timeline": {"native": {"timestamp_data": "fixture"}}, "intervals": [
                {"wall_start": 10000, "wall_end": 90000, "media_start": 0, "media_end": 80000}]}}
    catalog = Catalog(tmp_path)
    board = VideoBoard(engine_factory=RendererBoundary)
    board.timer.stop()
    board.select(["A"])
    board.configure(catalog, [row], VideoTimeline(intervals_from_rows([row])))
    board.reference_ms = 22000
    board.playing = True
    tile = board.tiles["A"]
    tile.asset_id = row["asset_id"]
    tile.interval = board.timeline.intervals[0]
    try:
        yield board, tile, source
    finally:
        board.close()
        catalog.close()
        app.processEvents()


def test_cache_error_falls_back_to_source_without_claiming_an_old_frame(player):
    board, tile, source = player
    board.compatibility = True
    tile.ready = False
    tile.pending = {"phase": "compat_cache", "asset": tile.asset_id, "generation": board.generation}
    board._compatibility_ready((board.generation, tile, tile.interval, 12000,
                                time.perf_counter(), "sample rate not set"))
    assert tile.engine is not None, "A derived cache failure must still try the original video"
    assert tile.engine._path == str(source)
    assert tile.pending["phase"] == "priming" and not tile.ready
    assert board.reference_ms == 22000
    assert not board.evidence()[0]["frame_ready"]


def test_stale_cache_failure_cannot_replace_current_seek(player):
    board, tile, _ = player
    tile.pending = {"phase": "exact_frame", "generation": board.generation}
    board._compatibility_ready((board.generation - 1, tile, tile.interval, 0,
                                time.perf_counter(), "old error"))
    assert tile.engine is None and tile.pending["phase"] == "exact_frame"
    assert board.reference_ms == 22000


def test_failed_cache_returns_native_ps_to_normal_speed(player):
    board, tile, _ = player
    board.set_rate(4)
    board._compatibility_ready((board.generation, tile, tile.interval, 12000,
                                time.perf_counter(), "cache disk unavailable"))
    assert board.rate == 1, "Unpaced native PS must not inherit a remux-only fast playback rate"
    board.set_rate(2)
    assert board.rate == 1, "Fast rates must stay blocked until a verified cache becomes available"


def test_paused_frame_prepares_one_cache_shared_by_play_and_latest_seek(player, monkeypatch):
    board, tile, _ = player
    app = QApplication.instance()
    entered, release = threading.Event(), threading.Event()
    calls = []
    cache = board.compatibility_cache

    def delayed_cache(asset, source, metadata, **kwargs):
        calls.append(asset)
        entered.set()
        assert release.wait(5)
        assert not kwargs["cancelled"](), "Same-video play/seek must not restart full remux"
        cache.root.mkdir(parents=True, exist_ok=True)
        target = cache.root / (asset + ".mkv")
        target.write_bytes(b"derived video; real remux covered separately")
        index = MediaTimelineIndex(str(target), target.stat().st_size, target.stat().st_mtime_ns, 0, 40,
                                   (TimelineSegment(0, 80000, 0, 80000),), ())
        cache.entries[asset] = {"stamp": file_stamp(target), "timeline": index.to_dict()}
        return target

    monkeypatch.setattr(cache, "build", delayed_cache)
    board.playing = False
    tile.pending = {"phase": "exact_frame", "generation": board.generation, "target": 12000,
                    "start": time.perf_counter(), "cold": True}
    image = QImage(32, 18, QImage.Format.Format_RGB888)
    try:
        board._precise_ready((board.generation, tile.asset_id, tile, 12000, image, None))
        assert entered.wait(.5), "The first paused frame must prepare its main-view cache in the background"
        assert tile.ready and tile.pending is None
        board.play(True)
        board.seek(32000)
        assert tile.pending["phase"] == "compat_cache" and not tile.ready
        release.set()
        deadline = time.monotonic() + 3
        while (tile.engine is None or tile.pending["phase"] == "compat_cache") and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert calls == [tile.asset_id]
        assert tile.engine is not None and tile.engine._path.endswith(".mkv")
        assert tile.pending["target"] == 22000
        assert board.reference_ms == 32000 and not board.evidence()[0]["frame_ready"]
    finally:
        release.set()


@pytest.mark.parametrize("operation", ["switch_project", "close"])
def test_abandoned_preparation_cancels_without_blocking_or_reopening_video(player, monkeypatch, tmp_path, operation):
    board, tile, source = player
    cache = board.compatibility_cache
    entered, release = threading.Event(), threading.Event()
    cancelled_after_release = []

    def slow_cache(_asset, _source, _metadata, *, cancelled, **_kwargs):
        entered.set()
        assert release.wait(5)
        cancelled_after_release.append(cancelled())
        raise RuntimeError("old project job")

    monkeypatch.setattr(cache, "build", slow_cache)
    replacement = None
    try:
        board._compatibility_request(tile, tile.interval, 12000, source, {})
        job = board.compatibility_jobs[tile.asset_id]
        assert entered.wait(.5)
        started = time.perf_counter()
        if operation == "close":
            board.close()
        else:
            (tmp_path / "other-project").mkdir()
            replacement = Catalog(tmp_path / "other-project")
            board.configure(replacement, [], VideoTimeline([]))
        assert time.perf_counter() - started < .5, "The GUI must not join a slow remux task"
        release.set()
        job["future"].result(timeout=2)
        # Exercise even a completion already queued at the moment of switching.
        board._compatibility_prepared((cache, tile.asset_id, job, "old project job"))
        QApplication.instance().processEvents()
        assert cancelled_after_release == [True]
        assert tile.engine is None and not tile.ready
        assert board.reference_ms == 22000
        assert not board.compatibility_failures
    finally:
        release.set()
        if replacement:
            replacement.close()


def test_quiet_preparation_failure_waits_for_explicit_play_retry(player):
    board, tile, _ = player
    board.playing = False
    tile.pending = None
    job = {"cancelled": False}
    board.compatibility_jobs[tile.asset_id] = job
    board._compatibility_prepared((board.compatibility_cache, tile.asset_id, job, "disk full"))
    assert tile.asset_id in board.compatibility_failures
    assert tile.engine is None and not board.playing
    # Starting playback is a deliberate retry; repeated paused seeks are not.
    board.play(True)
    assert tile.asset_id not in board.compatibility_failures
    assert tile.pending["phase"] == "compat_cache"


def test_prepared_video_only_cache_reports_missing_audio_once_on_use(player):
    board, tile, _ = player
    cache = board.compatibility_cache
    cache.root.mkdir(parents=True, exist_ok=True)
    target = cache.root / (tile.asset_id + ".mkv")
    target.write_bytes(b"derived video-only fixture")
    index = MediaTimelineIndex(str(target), target.stat().st_size, target.stat().st_mtime_ns, 0, 40,
                               (TimelineSegment(0, 80000, 0, 80000),), ())
    cache.entries[tile.asset_id] = {"stamp": file_stamp(target), "timeline": index.to_dict(), "audio_omitted": True}
    notices = []
    board.notice.connect(notices.append)
    board._request(tile, tile.interval, 12000)
    board._request(tile, tile.interval, 22000)
    assert tile.engine._path == str(target)
    assert len(notices) == 1 and "音轨" in notices[0] and "原片保留" in notices[0]
