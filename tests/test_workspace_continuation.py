import copy
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.coverage import continuation_target, video_coverage
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.window import MainWindow
from cowmata_tailring.workspace.work import SessionWork


def video(camera="A", start=10000, end=20000, state="ready", asset="a"):
    return {"path": camera + ".mp4", "kind": "video", "asset_id": asset, "state": state,
            "metadata": {"camera": camera, "duration_ms": end - start,
                "intervals": [{"media_start": 0, "media_end": end - start,
                               "wall_start": start, "wall_end": end, "verified": True}]}}


def status(rows, when, selected=("A",), complete=True, aligned=True):
    timeline = VideoTimeline(intervals_from_rows(rows))
    return video_coverage(timeline, rows, when, selected, scan_complete=complete, aligned=aligned)


def test_coverage_uses_all_views_and_actual_gaps():
    rows = [video(), video("B", 20000, 30000, asset="b")]
    assert status(rows, 22000).code == "other_views"
    assert status(rows, 22000).cameras == ("B",)
    assert status(rows, 5000).next_ms == 10000
    assert status(rows, 5000).code == "gap"
    assert status(rows, 30000).code == "exhausted"
    assert status(rows, 30000, aligned=False).code == "alignment_unknown"


@pytest.mark.parametrize("state, expected", [("pending", "indexing"), ("invalid", "unresolved_video"),
                                              ("review", "unresolved_video")])
def test_unresolved_index_cannot_claim_no_future_video(state, expected):
    assert status([video(state=state)], 30000).code == expected


def test_partial_scan_and_partial_manual_span_cannot_claim_end():
    assert status([video()], 30000, complete=False).code == "indexing"
    row = video()
    row["metadata"]["duration_ms"] = 15000
    assert status([row], 30000).code == "unresolved_video"
    assert status([], 30000).code == "no_videos"
    assert status([video()], 19999).code == "covered"


def imu(asset="b", device="D"):
    return {"kind": "imu", "state": "ready", "asset_id": asset, "path": asset + ".json",
            "metadata": {"device": device, "duration_ms": 10000, "create_time_ms": -999999}}


def calibrated(asset="b", cow="C", start=20000):
    work = SessionWork(asset)
    work.project.cow_id = cow
    work.clock = ClockMap([Anchor(0, start), Anchor(10000, start + 10000)])
    return work.to_dict()


def test_continuation_ignores_receive_time_and_duplicate_copy():
    row = imu()
    target, reason = continuation_target([row, copy.deepcopy(row)], "a", "D", "C", 22000,
                                        lambda _: calibrated())
    assert reason == "ready" and target["asset_id"] == "b"


@pytest.mark.parametrize("case", ["cow", "device", "unconfirmed", "gap", "overlap", "empty_cow", "break"])
def test_continuation_never_guesses_identity_or_time(case):
    rows = [imu()]
    work = calibrated()
    cow = "C"
    when = 22000
    if case == "cow":
        work["project"]["cow_id"] = "other"
    elif case == "device":
        rows[0]["metadata"]["device"] = "other"
    elif case == "unconfirmed":
        work["clock"]["anchors"] = []
    elif case == "gap":
        when = 19000
    elif case == "overlap":
        rows.append(imu("c"))
    elif case == "empty_cow":
        cow = ""
    else:
        work["clock"]["breaks"] = [[1000, 3000]]
    target, _ = continuation_target(rows, "a", "D", cow, when, lambda _: work)
    assert target is None


def test_sidecars_and_non_imu_json_are_not_processing_jobs(tmp_path):
    for name in ("001.pic", "001.jpg", "001.index", "说明.txt", "001.mp4.download", "backup.zip"):
        (tmp_path / name).write_text("unused")
    (tmp_path / "camera-settings.json").write_text('{"camera": 1}')
    catalog = Catalog(tmp_path, stability_seconds=0)
    assert catalog.scan(now=100).added == ["camera-settings.json"]
    result = catalog.index_one("camera-settings.json", SourceInspector(catalog.root, catalog.meta), now=101)
    assert result["state"] == "ignored"
    assert not catalog.pending(now=1000)
    assert len(list(tmp_path.glob("001.*"))) == 4
    catalog.close()


def test_json_arrays_are_ignored_but_damaged_imu_remains_visible(tmp_path):
    (tmp_path / "list.json").write_text('[{"camera":1}]')
    (tmp_path / "damaged.json").write_text('{"imu":"not-valid-base64", "version":2}')
    catalog = Catalog(tmp_path, stability_seconds=0)
    catalog.scan(now=100)
    inspector = SourceInspector(catalog.root, catalog.meta)
    assert catalog.index_one("list.json", inspector, now=101)["state"] == "ignored"
    assert catalog.index_one("damaged.json", inspector, now=101) is None
    assert {r["path"]: r["state"] for r in catalog.rows()}["damaged.json"] == "invalid"
    catalog.close()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_delivered_handoff_keeps_video_clock_layout_and_play_state(tmp_path, app, monkeypatch):
    window = MainWindow()
    window.board.timer.stop()
    window.source_timer.stop()
    window.save_timer.stop()
    window.catalog = Catalog(tmp_path, stability_seconds=0)
    first, second = "a" * 64, "b" * 64
    window.work = SessionWork.from_dict(calibrated(first, start=10000))
    window.current_row = imu(first)
    target = imu(second)
    window.rows = [target]
    atomic_json(window.catalog.work_path(second), calibrated(second))
    window.board.reference_ms = 22000
    window.board.playing = True
    window.board.rate = 2
    window.active_event = {"label": 0, "start": 19000, "assets": {first}, "cow_id": "C"}
    monkeypatch.setattr("cowmata_tailring.workspace.window.file_stamp", lambda _: "test-stamp")
    calls = []
    monkeypatch.setattr(window.board, "seek", lambda *_: calls.append("seek"))
    monkeypatch.setattr(window.board, "play", lambda *_: calls.append("play"))
    monkeypatch.setattr(window.board, "select", lambda *_: calls.append("select"))
    motion = SimpleNamespace(device="D", uid=1, duration_ms=10000, create_time_ms=0,
        capture_timing=lambda: {"revision": 1},
        times_ms=np.arange(0, 10001, 1000), plot_series=lambda: [],
        nearest_sample_index=lambda value: int(round(value / 1000)))
    window._motion_loaded((window.load_generation, target, motion, "test-stamp", True))
    assert window.work.asset_id == second and window.imu_ms == 2000
    assert window.board.reference_ms == 22000 and window.board.playing and window.board.rate == 2
    assert not calls
    assert window.active_event["assets"] == {first, second}
    assert window.work.clock.anchors[0].reference_ms == 20000
    window.motion = motion
    window.linked = True
    window.board.reference_ms = 32000
    window.video_time_changed(32000)
    deadline = time.monotonic() + 2
    while window._continuation_pending and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    prompt = window.alignment_label.text()
    assert "未强行拼接" in prompt
    window.video_time_changed(32050)
    assert window.alignment_label.text() == prompt
    window.board.reference_ms = 22000
    # A stale completion is ignored, and an uncalibrated late result cannot take over.
    window._motion_loaded((window.load_generation - 1, imu(first), motion, "stale", True))
    assert window.work.asset_id == second
    window._motion_loaded((window.load_generation, imu("c" * 64), motion, "late", True))
    assert window.work.asset_id == second
    window.active_event = None
    window.dirty = False
    window.close()


def test_handoff_search_is_background_and_unlink_cancels_it(tmp_path, app, monkeypatch):
    window = MainWindow()
    window.board.timer.stop()
    window.source_timer.stop()
    window.save_timer.stop()
    window.catalog = Catalog(tmp_path, stability_seconds=0)
    window.work = SessionWork.from_dict(calibrated("a" * 64))
    window.current_row = imu("a" * 64)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    thread_ids = []

    def slow_search(*_args):
        thread_ids.append(threading.get_ident())
        started.set()
        release.wait(2)
        done.set()
        return None, "no_calibrated_record"

    monkeypatch.setattr("cowmata_tailring.workspace.window.continuation_target", slow_search)
    generation = window.load_generation
    window.continue_motion(32000)
    assert started.wait(1) and thread_ids[0] != threading.get_ident()
    assert window._continuation_pending == "search"
    window.toggle_link(False)
    assert window.load_generation > generation and window._continuation_pending is None
    message = window.alignment_label.text()
    release.set()
    assert done.wait(1)
    app.processEvents()
    window._continuation_ready((generation, window.catalog, "a" * 64, "C", imu(), "ready"))
    assert window.work.asset_id == "a" * 64 and window.alignment_label.text() == message
    window.dirty = False
    window.close()


def test_first_record_load_does_not_overwrite_saved_view_preferences(tmp_path, app, monkeypatch):
    window = MainWindow()
    window.board.timer.stop()
    window.source_timer.stop()
    window.save_timer.stop()
    window.catalog = Catalog(tmp_path, stability_seconds=0)
    window.settings = {"selected_cameras": list("ABCDEFGH")}
    window.dirty = True  # Reference-clock setup emits a change before IMU load.
    calls = []

    def save():
        calls.append("saved")
        window.dirty = False

    monkeypatch.setattr(window, "save_current", save)
    monkeypatch.setattr("cowmata_tailring.workspace.window.threading.Thread",
                        lambda **_: SimpleNamespace(start=lambda: calls.append("load")))
    item = QListWidgetItem("test")
    item.setData(Qt.ItemDataRole.UserRole, imu("a" * 64))
    window.select_record(item)
    assert calls == ["load"] and window.settings["selected_cameras"] == list("ABCDEFGH")
    window.work = SessionWork("b" * 64)
    window.select_record(item)
    assert calls == ["load", "saved", "load"]  # Old annotations still save on switch.
    window.close()
