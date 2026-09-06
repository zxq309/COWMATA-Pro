import copy
import threading
import time

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_label_file import source as source

from cowmata_tailring.workspace.candidate_window import CandidateWindow
from cowmata_tailring.workspace.event_models import (
    available_packs,
    normalize_output,
    safe_child,
    verify_model,
)
from cowmata_tailring.workspace.label_file import build_label_file, load_history, save_label_file


@pytest.mark.parametrize("model", available_packs()[0]["models"], ids=lambda m: m["id"])
@pytest.mark.skipif(not (available_packs()[0]["app_root"] / "model_runtime_20260906/python.exe").is_file(),
                    reason="Binary event runtime is a Release asset; exercised in portable acceptance")
def test_reviewed_pack_files_present_and_verified(model):
    assert verify_model(available_packs()[0], model).name == "python.exe"


def test_pack_path_cannot_escape(tmp_path):
    with pytest.raises(ValueError):
        safe_child(tmp_path, "../outside")


@pytest.mark.parametrize("column", ["spot_s", "around_s", "event_time_s", "center_s"])
def test_normalization_uses_relative_point_not_wall_clock_or_window_end(tmp_path, column):
    (tmp_path / "result.csv").write_text(f"{column},score,end_s,approx_time_bj\n12.5,0.81,99,2099-01-01 01:00:00\n", encoding="utf-8")
    spec = dict(output="result.csv", time_column=column, score_column="score", code="TEST")
    points, audit = normalize_output(tmp_path, spec, 20000)
    assert points[0]["point_ms"] == 12500
    assert points[0]["time_semantics"] == "approximate_point"
    assert points[0]["review_status"] == "pending"
    assert "end_ms" not in points[0]


@pytest.mark.parametrize("point,score", [("nan", ".8"), ("-1", ".8"), ("200", ".8"), ("10", "inf")])
def test_invalid_predictions_never_enter_timeline(tmp_path, point, score):
    (tmp_path / "result.csv").write_text(f"time,score\n{point},{score}\n")
    with pytest.raises(ValueError):
        normalize_output(tmp_path, dict(output="result.csv", time_column="time", score_column="score", code="TEST"), 100000)


def result_for(work):
    return dict(id="test-run", identity=dict(asset_id=work.asset_id, cow_id=work.project.cow_id),
                version="TEST-ONLY", model_title="Test event", audit={"quality": "unknown"},
                candidates=[dict(id="candidate-1", point_ms=100, score=.9, code="STANDING_UP", review_status="pending")])


@pytest.fixture
def gui(source):
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.modern_window import MainWindow
    app = QApplication.instance() or QApplication([])
    root, motion, work, rows = source
    window = MainWindow()
    for timer in (window.board.timer, window.save_timer, window.source_timer):
        timer.stop()
    window.catalog = Catalog(root)
    window.motion, window.work, window.rows = motion, work, rows
    dialog = CandidateWindow(window)
    window._candidate_window = dialog
    yield window, dialog, app
    dialog.cancel()
    deadline = time.monotonic() + 5
    while dialog.running and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    window.close()
    window.catalog.close()


def test_candidate_queue_roundtrip_is_separate_from_truth(gui, source, tmp_path):
    window, dialog, _ = gui
    before = copy.deepcopy(window.work.project.events)
    dialog.receive((dialog.token(), result_for(window.work)))
    assert window.work.project.events == before
    dialog.items.setCurrentRow(0)
    dialog.verdict("unknown")
    output = tmp_path / "anywhere.json"
    doc = build_label_file(window.work, window.motion, source[0], window.rows, {})
    save_label_file(output, doc)
    loaded = load_history(output)
    run = loaded.work.project.extras["event_model_runs"]["test-run"]
    assert run["candidates"][0]["review_status"] == "unknown"


@pytest.mark.parametrize("change", ["cow", "generation", "cancel"])
def test_stale_or_cancelled_candidate_result_is_discarded(gui, change):
    window, dialog, _ = gui
    token = dialog.token()
    result = result_for(window.work)
    if change == "cow":
        window.work.project.cow_id = "ANOTHER"
    elif change == "generation":
        window.load_generation += 1
    else:
        dialog.cancel()
    dialog.receive((token, result))
    assert not window.work.project.extras.get("event_model_runs")


def test_unknown_alignment_keeps_video_and_invalidates_old_selection(gui):
    from cowmata_tailring.workspace.clocks import ClockMap
    window, dialog, _ = gui
    window.work.clock = ClockMap()
    window.board.reference_ms = 12345
    window.selection = (0, 20)
    dialog.receive((dialog.token(), result_for(window.work)))
    dialog.items.setCurrentRow(0)
    dialog.review()
    assert window.board.reference_ms == 12345
    assert window.selection is None
    assert window.imu_ms == 100
    assert not window.work.drafts


def test_model_draft_requires_manual_range_and_retains_provenance(gui):
    window, dialog, _ = gui
    dialog.receive((dialog.token(), result_for(window.work)))
    dialog.items.setCurrentRow(0)
    dialog.make_draft()
    assert not window.work.drafts
    window.selection = (60, 360)
    dialog.make_draft()
    draft = window.work.drafts[-1]
    assert draft["reference_start"] == 10060 and draft["reference_end"] == 10360
    assert draft["model_candidate"]["candidate"]["point_ms"] == 100
    with pytest.raises(ValueError, match="视频"):
        window.work.confirm_draft(draft["id"], window.motion.duration_ms)


def test_background_cancellation_and_source_switch(gui, monkeypatch):
    window, dialog, app = gui
    entered = threading.Event()
    stopped = threading.Event()
    def slow(*args, cancelled, **kwargs):
        entered.set()
        while not cancelled():
            time.sleep(.01)
        stopped.set()
        raise InterruptedError("cancelled")
    monkeypatch.setattr("cowmata_tailring.workspace.candidate_window.predict_one", slow)
    dialog.start()
    assert entered.wait(1)
    window.load_generation += 1
    dialog.check_context()
    deadline = time.monotonic() + 3
    while dialog.running and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert stopped.is_set() and not dialog.running
    assert not window.work.project.extras.get("event_model_runs")


def test_inference_load_guard_releases_after_errors():
    from cowmata_tailring.workspace.event_models import inference_active, inference_load
    assert not inference_active()
    with pytest.raises(RuntimeError):
        with inference_load():
            assert inference_active()
            with inference_load():
                assert inference_active()
            raise RuntimeError("TEST ONLY")
    assert not inference_active()


def test_multiview_guard_updates_control_and_does_not_oscillate(gui):
    from cowmata_tailring.workspace.event_models import inference_load
    window, _, _ = gui
    board = window.board
    board.select(["A", "B", "C", "D"])
    board.playing = True
    reference = board.reference_ms
    with inference_load():
        board.set_policy("full")
        assert board.playback_policy == "balanced"
        assert window.playback_policy.currentIndex() == 1
        window.playback_policy.setCurrentIndex(0)
        assert board.playback_policy == "balanced" and window.playback_policy.currentIndex() == 1
        assert board.reference_ms == reference
    assert board.playback_policy == "balanced"
    window.playback_policy.setCurrentIndex(0)
    assert board.playback_policy == "full"


def test_two_views_are_not_forced_into_preview(gui):
    from cowmata_tailring.workspace.event_models import inference_load
    window, _, _ = gui
    board = window.board
    board.select(["A", "B"])
    board.playing = True
    with inference_load():
        board.set_policy("full")
        assert board.playback_policy == "full"
