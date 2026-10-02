# ruff: noqa: F811  (pytest fixtures shared from test_annotation_pipeline_v330)
"""4.4.6: the problems reported against 4.4.4 on 2026-10-02.

1. Pause → play (or clicking the IMU) continued a few seconds after the real position.
2. The same labelling gave some labels 已确认 and others 需复核.
3. Merely clicking an IMU record wrote a 标注.json into the project.
"""
import math
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.ui.interactive_plot import drag_threshold
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoInterval, VideoTimeline
from cowmata_tailring.workspace.playback import VideoBoard
from cowmata_tailring.workspace.storage import read_json
from cowmata_tailring.workspace.work import SessionWork

# --- 1: resume / seek continues from the requested moment ----------------------------------


class NvrEngine:
    """libVLC stand-in: seeks land on the next keyframe (every ``keyframe_ms``, or ``late_ms`` after the
    request when given) and the decoder runs on at its rate, like the NVR recordings in the report."""

    _force_avformat = False
    _dahua_duration_index = None
    current_status = "playing"

    def __init__(self, clock, *, keyframe_ms=4000, late_ms=None):
        self.clock, self.keyframe_ms, self.late_ms = clock, keyframe_ms, late_ms
        self.position, self.since, self.rate, self.paused, self.pictures = 0.0, clock(), 1.0, False, 0.0

    def _advance(self):
        now = self.clock()
        if not self.paused:
            self.position += (now - self.since) * 1000 * self.rate
            self.pictures += (now - self.since) * 25 * self.rate
        self.since = now

    def get_time_ms(self):
        self._advance()
        return int(self.position)

    def set_time_ms(self, value):
        self._advance()
        self.position = value + self.late_ms if self.late_ms is not None else math.ceil(value / self.keyframe_ms) * self.keyframe_ms
        return True

    def set_rate(self, rate):
        self._advance()
        self.rate = rate

    def pause(self, paused=True):
        self._advance()
        self.paused = bool(paused)

    def stats(self):
        self._advance()
        return SimpleNamespace(displayed_pictures=int(self.pictures), decoded_video=int(self.pictures))

    def video_output_count(self):
        return 1

    def is_seekable(self):
        return True

    def close(self):
        pass

    def set_volume(self, value):
        pass

    def clear_pending_seek(self):
        pass

    def play(self):
        self.pause(False)
        return True


def resume(rate, target, **engine):
    QApplication.instance() or QApplication([])
    board = VideoBoard()
    board.timer.stop()
    try:
        board.select(["A"])
        board.set_main("A")
        interval = VideoInterval("a" * 64, "a.mp4", "A", 0, 600_000, 0, 600_000, True)
        board.timeline = VideoTimeline([interval])
        tile = board.tiles["A"]
        tile.interval, tile.asset_id = interval, interval.asset_id
        now = [100.0]
        tile.engine = NvrEngine(lambda: now[0], **engine)
        board.playing, board.rate, board.reference_ms = True, rate, float(target)
        tile.pending = {"target": target, "generation": board.generation, "start": now[0], "baseline": 0,
                        "asset": interval.asset_id, "cold": False, "phase": "priming", "attempts": 0}
        for _ in range(600):
            board._observe(tile, now[0])
            if tile.ready:
                break
            now[0] += 0.04
        return board, tile
    finally:
        board.close()


@pytest.mark.parametrize("rate", [1.0, 4.0])
def test_playback_resumes_where_it_was_paused_even_when_the_seek_lands_on_a_later_keyframe(rate):
    board, tile = resume(rate, 20_500)  # the requested seek lands on the keyframe at 24 s
    assert tile.ready
    assert -board.EXACT_START_BEHIND_MS <= board.reference_ms - 20_500 <= board.EXACT_START_AHEAD_MS


def test_a_seek_that_never_lands_early_still_starts_after_bounded_retries():
    board, tile = resume(4.0, 20_500, late_ms=5000)
    assert tile.ready and tile.pending is None


# --- 2: a click selects a label; re-aligning keeps labels that did not move ----------------

def test_clicking_a_confirmed_label_with_a_shaky_hand_neither_moves_nor_reopens_it(workflow_window):
    window = workflow_window
    window.plot.wave._duration_ms = 1000
    window.plot.wave.set_view(0, 1000)
    event = window.work.project.add_event(0, 200, 600)
    event.extras["confirmation"] = "confirmed"
    window.refresh_events()
    window.show()
    QApplication.processEvents()
    track = window.plot.track
    track.repaint()
    rectangle = next(rect for rect, identifier in track.hits if identifier == event.id)
    point = QPoint(round(rectangle.center().x()), round(rectangle.center().y()))
    jitter = point + QPoint(drag_threshold() - 2, 0)
    QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(track, jitter, delay=20)
    QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=jitter)
    assert (event.t0, event.t1) == (200, 600) and event.extras["confirmation"] == "confirmed"
    assert window.selected_entry() == ("event", event.id)


def test_realigning_keeps_labels_whose_video_moment_did_not_move():
    work = SessionWork("a" * 64)
    work.clock = ClockMap([Anchor(0, 1_000_000), Anchor(600_000, 1_600_000)])
    kept = work.project.add_event(0, 1_000, 5_000)
    kept.extras["confirmation"] = "confirmed"
    work.set_clock(ClockMap([Anchor(0, 1_000_020), Anchor(600_000, 1_600_020)]))  # under one frame
    assert kept.extras["confirmation"] == "confirmed" and kept.extras["mapping_revision"] == work.clock.revision
    assert kept.extras["reference_start"] == work.clock.map(1_000)
    work.set_clock(ClockMap([Anchor(0, 1_002_000), Anchor(600_000, 1_602_000)]))  # 2 s: look again
    assert kept.extras["confirmation"] == "needs_review"


# --- 3: no 标注.json for a record nobody worked on ------------------------------------------

def test_browsing_a_record_writes_no_label_file_and_an_empty_one_is_removed(workflow_window):
    window = workflow_window
    path = window.catalog.work_path(window.work.asset_id, for_write=True)
    window.imu_ms = 400  # browsing moves the position; that is not annotation
    window.save_current()
    assert not path.exists()
    assert window.record_status(window.current_row) == "in_progress"  # the open record shows 正在标注
    window.work.project.add_event(0, 100, 300)
    window.save_current()
    assert path.is_file() and read_json(path)["project"]["events"]
    window.work.project.events.clear()  # every label deleted again: nothing left to keep
    window.save_current()
    assert not path.exists() and window.current_row["path"] not in window.settings.get("review_progress", {})


def test_completing_a_record_without_labels_keeps_its_label_file(workflow_window):
    window = workflow_window
    path = window.catalog.work_path(window.work.asset_id, for_write=True)
    window.work.progress["status"] = "done"  # 完成本份: a person decided there is nothing to label
    window.save_current()
    assert path.is_file() and read_json(path)["progress"]["status"] == "done"
