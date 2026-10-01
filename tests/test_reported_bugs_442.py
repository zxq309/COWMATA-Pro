# ruff: noqa: F811  (pytest fixtures shared from test_annotation_pipeline_v330)
import time
from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.workspace.catalog import Catalog, file_stamp
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.playback import VideoBoard
from cowmata_tailring.workspace.window import MainWindow as ControllerWindow


def test_confirm_selected_refreshes_stale_ready_evidence(workflow_window, monkeypatch):
    window = workflow_window
    row = next(r for r in window.rows if r["kind"] == "video")
    window.work.project.cow_id = "COW-442"
    window.work.clock = ClockMap([Anchor(0, 10000), Anchor(window.motion.duration_ms, 11000)])
    draft = window.work.add_draft(0, 10100, 10200, [{
        "camera": "A", "asset_id": row["asset_id"], "frame_ready": False, "verified_interval": False,
    }])
    fresh = [{
        "camera": "A", "asset_id": row["asset_id"], "frame_ready": True, "verified_interval": True,
        "reference_ms": 10100, "media_ms": 100, "frame_source": "ffmpeg_pts",
        "camera_mapping_revision": "uncalibrated", "video_revision": window.video_revision(row),
    }]
    monkeypatch.setattr(window, "evidence", lambda: fresh)
    window.refresh_events(preferred=("draft", draft["id"]))
    window.events.selectRow(0)
    window.board.reference_ms = 10150  # paused on the action itself
    window.confirm_selected()
    assert len(window.work.project.events) == 1
    assert any(item.get("verified_interval") for item in window.work.project.events[0].extras["video_evidence"])


def test_edit_source_reports_imu_rows_and_pending_video_rows_clearly(workflow_window, monkeypatch):
    window = workflow_window
    video_row = next(r for r in window.rows if r["kind"] == "video")
    pending = {**video_row, "state": "pending", "error": "", "metadata": dict(video_row["metadata"])}
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    window.edit_source(window.current_row)
    window.edit_source(pending)
    assert "九轴 JSON" in messages[0] and "录像行" in messages[0]
    assert "重新建立所选视频索引" in messages[1]


def test_dragging_pip_resize_handle_switches_to_custom_and_persists(qt_application, monkeypatch):
    window = MainWindow()
    for timer in (window.save_timer, window.source_timer, window.board.timer):
        timer.stop()
    window.resize(1400, 900)
    window.show()
    window.set_presentation("C", persist=False)
    qt_application.processEvents()
    original = window.stage.video.size()
    handle = window.stage.resize_handle
    start = handle.rect().center()
    finish = QPoint(start.x() + 90, start.y() + 70)
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(handle, finish)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=finish)
    qt_application.processEvents()
    assert window.stage.video.width() > original.width()
    assert window.pip_size.currentIndex() == 3
    monkeypatch.setattr(ControllerWindow, "save_current", lambda self, background=False: None)
    window.catalog = SimpleNamespace(readonly=False)
    window.settings = {}
    window.save_current()
    prefs = window.settings["presentation"]
    assert prefs["pip_size"] == 3
    assert prefs["pip_custom_size"][0] == window.stage.video.width()
    restored = MainWindow()
    for timer in (restored.save_timer, restored.source_timer, restored.board.timer):
        timer.stop()
    restored.resize(1400, 900)
    restored.show()
    restored.restore_presentation(prefs)
    qt_application.processEvents()
    assert restored.pip_size.currentIndex() == 3
    assert restored.stage.pip_custom_size is not None
    assert restored.stage.pip_custom_size.width() == prefs["pip_custom_size"][0]
    restored.close()
    window.close()
    QApplication.instance().processEvents()


def test_precise_pause_updates_reference_clock_before_play_resume(qt_application, tmp_path):
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"fixture video identity")
    row = {"state": "ready", "asset_id": "a" * 64, "path": source.name,
           "stamp": file_stamp(source), "metadata": {"camera": "A", "format": "mpeg", "intervals": [
               {"wall_start": 10000, "wall_end": 90000, "media_start": 0, "media_end": 80000, "verified": True}]}}
    catalog = Catalog(tmp_path)
    board = VideoBoard()
    board.timer.stop()
    board.select(["A"])
    board.configure(catalog, [row], VideoTimeline(intervals_from_rows([row])))
    tile = board.tiles["A"]
    tile.asset_id = row["asset_id"]
    tile.interval = board.timeline.intervals[0]
    tile.pending = {"cold": True, "start": time.perf_counter(), "target": 12000, "generation": board.generation}
    board.reference_ms = 22000
    image = QImage(32, 18, QImage.Format.Format_RGB888)
    expected = board.timeline.reference_time("A", tile.interval.wall_at(12000))
    board._precise_ready((board.generation, tile.asset_id, tile, 12000, image, None))
    assert board.reference_ms == expected
    resumed = []
    board.seek = lambda value: resumed.append(value)
    board.play(True)
    assert resumed == [expected]
    board.close()
    catalog.close()
    qt_application.processEvents()
