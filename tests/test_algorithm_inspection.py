import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
from cowmata_tailring.workspace.algorithm_catalog import BEHAVIORS, HEALTH, bindings
from cowmata_tailring.workspace.event_models import available_packs
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.board.timer.stop()
    w.source_timer.stop()
    w.save_timer.stop()
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


def test_menu_order_and_one_to_one_codes(window):
    titles = [a.text().split("(")[0] for a in window.menuBar().actions()]
    assert titles == ["文件", "编辑", "视图", "工具", "数据整理", "行为识别", "健康与繁殖", "帮助"]
    assert len({s.code for s in BEHAVIORS}) == 8
    labels = {label["code"] for label in DEFAULT_LABELS}
    assert {s.code for s in BEHAVIORS} <= labels
    assert len(HEALTH) == 4
    assert not any(bindings(s, available_packs()) for s in HEALTH)
    assert sum(bool(bindings(s, available_packs())) for s in BEHAVIORS) == 5


@pytest.mark.parametrize("spec", BEHAVIORS + HEALTH)
def test_every_algorithm_locks_one_view_and_returns_layout(window, spec):
    window.board.select(["A", "B", "C"])
    window.set_presentation("B")
    window.playback_policy.setCurrentIndex(1)
    window.board.reference_ms = 123450
    window.open_algorithm(spec)
    app = QApplication.instance()
    app.processEvents()
    assert window.board.single_camera_only
    assert window.algorithm_panel.width() >= 280 and window.algorithm_panel.isVisible()
    assert window.board.playback_policy == "focus"
    assert [k for k, t in window.board.tiles.items() if not t.isHidden()] == ["A"]
    window.algorithm_panel.camera.setCurrentIndex(1)
    app.processEvents()
    assert window.board.main_camera == "B" and window.board.reference_ms == 123450
    assert [k for k, t in window.board.tiles.items() if not t.isHidden()] == ["B"]
    assert window.board.is_preview("A") and not window.board.is_preview("B")
    window.board.enlarge("B")
    window.set_presentation("B")
    window.board.set_policy("full")
    app.processEvents()
    assert sum(not t.isHidden() for t in window.board.tiles.values()) == 1
    assert window.board.playback_policy == "focus"
    assert window.stage.mode == "A"
    if not bindings(spec, available_packs()):
        assert not window.algorithm_panel.run_button.isEnabled()
        assert "待接入" in window.algorithm_panel.status.text()
    window.exit_algorithm()
    app.processEvents()
    assert not window.board.single_camera_only
    assert window.stage.mode == "B" and window.board.playback_policy == "balanced"
    assert sum(not t.isHidden() for t in window.board.tiles.values()) == 3


def test_inspection_output_not_candidate_or_manual_label(window, monkeypatch):
    w = window
    w.work = SessionWork("source-id")
    w.work.project.cow_id = "test-cow"
    monkeypatch.setattr(w, "writable_work", lambda: True)
    monkeypatch.setattr(w, "save_current", lambda *a, **kw: None)
    w.open_algorithm(BEHAVIORS[0])
    p = w.algorithm_panel
    p.cancelled.clear()
    result = {"id": "r1", "identity": {"cow_id": "test-cow", "model_id": "stand_up"},
              "version": "test", "candidates": [{"point_ms": 10, "score": .2}], "audit": {}}
    p.receive((p.token(), copy.deepcopy(result), None, False))
    assert len(w.work.project.extras["algorithm_inspections"]) == 1
    assert "event_model_runs" not in w.work.project.extras
    assert not w.work.project.events and not w.work.drafts
    assert p.items.count() == 1
    p.set_algorithm(BEHAVIORS[1])
    assert p.items.count() == 0
    p.receive((("stale",), result, None, False))
    assert len(w.work.project.extras["algorithm_inspections"]) == 1
    p.cancelled.clear()
    p.receive((p.token(), result, None, True))
    assert p.items.count() == 0


def test_background_algorithm_and_candidate_jobs_do_not_overlap(window):
    from cowmata_tailring.workspace.candidate_window import CandidateWindow
    panel = window.algorithm_panel
    panel.running = True
    window.open_candidates()
    assert window._candidate_window is None
    candidate = CandidateWindow(window)
    candidate.start()
    assert not candidate.running and "独立算法" in candidate.status.text()
    candidate.timer.stop()
    panel.running = False
    window._candidate_window = SimpleNamespace(running=True)
    window.open_algorithm(BEHAVIORS[0])
    assert window._algorithm_restore is None
    window._candidate_window = None


def test_old_label_order_and_missing_mounting_shortcut_are_safe(window, monkeypatch):
    window.work = SessionWork("old")
    window.work.project.labels = window.work.project.labels[:17]
    window.refresh_events()
    assert window.labels.count() == 17
    called = []
    monkeypatch.setattr(window, "mark", called.append)
    window.mark_code("MOUNTING")
    assert not called
    window.work.project.labels.reverse()
    window.refresh_events()
    window.mark_code("STANDING")
    assert called == [16]


def test_compact_package_excludes_only_audited_tools():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("portable_builder", root / "scripts/build_portable.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {"Qt6WebEngineCore.dll", "QtWidgets.pyd", "Qt6Svg.dll", "Qt6Core.dll", "opengl32sw.dll", "QtWebEngineProcess.exe", "plugins", "include", "qml"}
    excluded = module.portable_ignore(root / "runtime/Lib/site-packages/PySide6", names)
    assert excluded >= {"Qt6WebEngineCore.dll", "QtWebEngineProcess.exe", "include"}
    assert not excluded & {"QtWidgets.pyd", "Qt6Svg.dll", "Qt6Core.dll", "opengl32sw.dll", "plugins", "qml"}
    assert module.portable_ignore(root / "vendor/ffmpeg/bin", {"ffplay.exe", "ffmpeg.exe", "ffprobe.exe"}) == {"ffplay.exe"}
