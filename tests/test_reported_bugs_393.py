# ruff: noqa: F811 -- pytest fixture injection
"""Regressions for the five issues reported in 3.9.2 bug.docx."""

import copy

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHelpEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolTip
from test_annotation_pipeline_v330 import case, window  # noqa: F401

from cowmata_tailring.annotation.core import Label
from cowmata_tailring.annotation.defaults import DEFAULT_LABELS, LEGACY_DEFAULT_LABELS
from cowmata_tailring.ui.widgets import PlotSeries


def test_open_long_record_shows_whole_timeline_and_restores_position(window, monkeypatch):
    from cowmata_tailring.workspace.storage import atomic_json

    motion = window.motion
    motion.times_ms = motion.times_ms * 3600
    motion.duration_ms = 3600000
    window.work.progress["imu_ms"] = 2592000
    atomic_json(window.catalog.work_path(window.work.asset_id), window.work.to_dict())
    monkeypatch.setattr(window, "request_record_videos", lambda **kw: None)
    monkeypatch.setattr(window, "update_coverage", lambda **kw: None)
    window._motion_loaded(
        (window.load_generation, window.current_row, motion, window.current_stamp, False)
    )
    assert window.plot.view_range == (0, 3600000)
    assert window.imu_ms == 2592000


def waveform(window):
    wave = window.plot.wave
    wave.resize(900, 350)
    times = np.arange(0, 10001, 20, dtype=float)
    wave.set_data(
        [PlotSeries("ax", "AX", "g", "#159c8d", times, np.full(len(times), 1.234))], 10000
    )
    wave.set_events(
        [{"name": "test", "color": "#159c8d"}], [{"id": 1, "li": 0, "t0": 3000, "t1": 5000}]
    )
    wave.set_selected_event(1)
    wave.set_view(2000, 7000)
    return wave


@pytest.mark.parametrize("mode", ["shift", "button"])
def test_pan_over_selected_boundary_preserves_annotation(window, mode):
    wave = waveform(window)
    before = copy.deepcopy(wave._events)
    selected = []
    wave.rangeSelected.connect(lambda *args: selected.append(args))
    if mode == "button":
        window.plot.pan_button.click()
    modifiers = (
        Qt.KeyboardModifier.ShiftModifier if mode == "shift" else Qt.KeyboardModifier.NoModifier
    )
    point = QPointF(wave._x_for_time(3000), wave._plot_rect().center().y())
    for kind, p, button, buttons in [
        (QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
        (
            QEvent.Type.MouseMove,
            point + QPointF(80, 0),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        ),
        (
            QEvent.Type.MouseButtonRelease,
            point + QPointF(80, 0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        ),
    ]:
        QApplication.sendEvent(wave, QMouseEvent(kind, p, p, button, buttons, modifiers))
    assert wave.view_range[0] < 2000
    assert wave._events == before
    assert selected == []


def test_full_record_button_resets_zoom_without_moving_playhead(window):
    wave = waveform(window)
    wave.set_playhead(3500)
    window.plot.full_button.click()
    assert wave.view_range == (0, 10000)
    assert wave._playhead_ms == 3500


def test_stationary_hover_keeps_actual_sample_values(window):
    wave = waveform(window)
    point = QPoint(round(wave._x_for_time(4000)), round(wave._plot_rect().center().y()))
    event = QHelpEvent(QEvent.Type.ToolTip, point, wave.mapToGlobal(point))
    QApplication.sendEvent(wave, event)
    assert "1.234 g" in QToolTip.text()
    assert "AX" in QToolTip.text()
    QTest.qWait(900)
    QApplication.sendEvent(wave, QHelpEvent(QEvent.Type.ToolTip, point, wave.mapToGlobal(point)))
    assert "1.234 g" in QToolTip.text()
    assert "Shift" not in QToolTip.text()
    QToolTip.hideText()


LEGACY = [
    ("STANDING", "G"),
    ("LYING", "H"),
    ("WALKING", "I"),
    ("STRAINING_ONSET", "J"),
    ("TAIL_RAISED", "Q"),
    ("TAIL_WAGGING", "W"),
]


@pytest.mark.parametrize("code,key", LEGACY)
def test_legacy_events_remain_but_do_not_create_new_labels(window, monkeypatch, code, key):
    window.work.project.labels = [Label.from_dict(x) for x in LEGACY_DEFAULT_LABELS]
    index = next(i for i, label in enumerate(window.work.project.labels) if label.code == code)
    old = window.work.project.add_event(index, 10, None if code == "STRAINING_ONSET" else 20)
    identity = old.id
    window.refresh_events()
    kept = window.work.project.event_by_id(identity)
    label = window.work.project.labels[kept.li]
    assert label.code == code and label.key == "" and not label.trainable
    assert kept.t0 == 10
    assert window.labels.count() == len(DEFAULT_LABELS)
    called=[]
    monkeypatch.setattr(window, "mark", called.append)
    window.mark_code(code)
    assert called == []


def test_empty_candidate_window_explains_model_setup_and_refreshes_after_import(
    window, monkeypatch
):
    from cowmata_tailring.workspace import candidate_window as module

    packs = []
    monkeypatch.setattr(module, "available_packs", lambda: packs)
    opened = []
    monkeypatch.setattr(window, "open_behavior_390", lambda: opened.append(True))
    panel = module.CandidateWindow(window)
    try:
        assert "导入" in panel.status.text() and "模型" in panel.status.text()
        assert not panel.start_button.isEnabled()
        panel.model_setup_button.click()
        assert opened == [True]
        packs.append(
            {
                "version": "qa-model",
                "hash": "a" * 64,
                "modality": "motion",
                "models": [{"id": "STANDING_UP", "title": "起立过程"}],
            }
        )
        panel.refresh_button.click()
        assert panel.start_button.isEnabled()
        assert panel.versions.currentText() == "qa-model"
        assert panel.models.count() == 2
        packs.clear()
        panel.refresh_button.click()
        assert not panel.start_button.isEnabled()
        assert "导入" in panel.status.text()
    finally:
        panel.timer.stop()
        panel.close()


def test_candidate_refresh_on_activation_preserves_selected_model(window, monkeypatch):
    from cowmata_tailring.workspace import candidate_window as module

    packs = []
    monkeypatch.setattr(module, "available_packs", lambda: packs)
    panel = module.CandidateWindow(window)
    try:
        packs.append(
            {
                "version": "qa-model",
                "hash": "a" * 64,
                "modality": "motion",
                "models": [{"id": "STANDING_UP", "title": "起立过程"}],
            }
        )
        QApplication.sendEvent(panel, QEvent(QEvent.Type.WindowActivate))
        assert panel.start_button.isEnabled()
        panel.models.setCurrentIndex(1)
        QApplication.sendEvent(panel, QEvent(QEvent.Type.WindowActivate))
        assert panel.models.currentData() == "STANDING_UP"
    finally:
        panel.timer.stop()
        panel.close()


def test_cancel_missing_video_directory_still_loads_raw_record(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    from cowmata_tailring.workspace.standalone import StandaloneCatalog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: "")
    opened = []

    def open_project(root, *, preferred_json, day, standalone):
        catalog = StandaloneCatalog(
            **standalone, day=day, meta_path=tmp_path / "single-cache", stability_seconds=0
        )
        try:
            catalog.scan(now=1)
            opened.extend(r["path"] for r in catalog.rows())
        finally:
            catalog.close()

    monkeypatch.setattr(window, "open_project", open_project)
    window.open_standalone(window.motion.source_path)
    assert "record.json" in opened
    assert not (window.motion.source_path.parent / "Video").exists()
