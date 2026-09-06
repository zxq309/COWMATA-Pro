import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.signal_panel import ReviewWaveform, SignalPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    w.board.timer.stop()
    w.save_timer.stop()
    w.source_timer.stop()
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


@pytest.mark.parametrize("size", [(1280, 800), (1600, 1000), (1920, 1080)])
@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_modes_keep_handles_clock_and_fit(window, app, mode, size):
    window.resize(*size)
    window.board.select([f"camera-{i}" for i in range(8)])
    window.board.reference_ms = 100000
    window.board.rate = 2
    before = (window.board.generation, window.board.reference_ms, window.board.rate,
              {c: int(t.surface.winId()) for c, t in window.board.tiles.items()})
    window.set_presentation(mode)
    app.processEvents()
    assert (window.board.generation, window.board.reference_ms, window.board.rate,
            {c: int(t.surface.winId()) for c, t in window.board.tiles.items()}) == before
    # resize()/geometry() use logical pixels. At 150%, a 1080p desktop
    # is only 720 logical pixels high; Windows can clamp oversized requests.
    # Keep the non-collapse and containment checks without assuming 100% DPI.
    minimum_height = min(size[1], window.screen().availableGeometry().height()) - 80
    assert window.width() == size[0] and minimum_height <= window.height() <= size[1]
    assert window.stage.rect().contains(window.stage.video.geometry())
    assert window.stage.rect().contains(window.stage.signal_panel.geometry())
    if mode == "B":
        assert all(window.board.rect().contains(t.geometry()) for t in window.board.tiles.values())
        dpr = window.devicePixelRatioF()
        assert all(t.width() * dpr >= 200 and t.height() * dpr >= 140 for t in window.board.tiles.values())
    assert window.plot.wave._lane_count() == 0
    assert window.plot.track.parentWidget() is not window.plot.wave
    assert len(window.board.pool) == 8


def test_pip_drag_and_size_are_clamped(window, app):
    window.set_presentation("C")
    for size in range(3):
        window.pip_size.setCurrentIndex(size)
        for point in (QPoint(-9999, -9999), QPoint(9999, 9999)):
            window.stage.move_pip(point)
            assert window.stage.rect().contains(window.stage.video.geometry())
    window.reset_pip()
    assert window.stage.pip_position == (1, 0)


def test_presentation_restore_and_bad_preferences(window):
    window.restore_presentation({"mode": "C", "pip_size": 2, "wave_size": 0,
                                 "pip_position": [.2, .7], "signal_group": 2,
                                 "sources_open": True, "events_open": False})
    assert window.stage.mode == "C" and window.stage.pip_scale == .46
    assert window.stage.pip_position == (.2, .7)
    assert window.plot.wave.group == "g" and window.source_panel.isVisible()
    for invalid in (None, [], {"mode": [], "pip_size": "bad", "wave_size": -20, "pip_position": [None, "bad"]}):
        window.restore_presentation(invalid)
        assert window.stage.mode == "A" and window.pip_size.currentIndex() == 1
        assert window.stage.pip_position == (1.0, 0.0)


def test_legacy_columns_cannot_collapse_modern_grid(window, app):
    window.resize(1280, 800)
    window.board.select([f"camera-{i}" for i in range(8)])
    window.set_layout(1)
    window.set_presentation("B")
    app.processEvents()
    tiles = list(window.board.tiles.values())
    assert len({t.y() for t in tiles}) == 2
    assert all(t.height() * window.devicePixelRatioF() >= 140 for t in tiles)


def test_main_width_control_relayouts_without_seek(window, app):
    window.board.select(["A", "B"])
    before = window.board.generation
    window.board.observation_ratio = 60
    window.board.relayout()
    assert window.board.generation == before
    assert abs(window.board.tiles["A"].width() / window.board.width() - .60) < .02


def test_pip_enlarge_really_opens_main_view_without_reload(window):
    window.board.select(["A", "B"])
    before = window.board.generation
    window.set_presentation("C")
    handles = {c: int(t.surface.winId()) for c, t in window.board.tiles.items()}
    window.board.enlarge("B")
    assert window.stage.mode == "A" and window.board.expanded == "B"
    assert window.board.main_camera == "B" and window.board.generation == before
    assert handles == {c: int(t.surface.winId()) for c, t in window.board.tiles.items()}
    window.board.enlarge(None)
    assert window.board.expanded is None


def test_typing_digits_space_and_f_does_not_trigger_shortcuts(window, app):
    calls = []
    window.mark = lambda index: calls.append(index)
    window.board.play = lambda *args: calls.append("play")
    window.toggle_sources()
    window.activateWindow()
    window.cow.setFocus()
    app.processEvents()
    QTest.keyClicks(window.cow, "Cow 12345 F0")
    assert window.cow.text() == "Cow 12345 F0"
    assert calls == []


def test_all_source_and_detail_controls_remain_accessible(window, app):
    assert window.source_panel.isHidden() and window.event_panel.isHidden()
    window.toggle_sources()
    window.toggle_events()
    app.processEvents()
    assert window.source_panel.isVisible() and window.event_panel.isVisible()
    assert window.records.isVisible() and window.events.isVisible()
    assert window.strict.parentWidget() == window.options
    assert window.compatibility.parentWidget() == window.options


def test_hover_never_interpolates_gap_or_changes_samples(app):
    wave = ReviewWaveform()
    times = np.array([0, 20, 40, 300, 320], dtype=float)
    values = np.array([1, 2, 3, 4, 5], dtype=float)
    series = PlotSeries("ax", "AX", "m/s2", "#159c8d", times, values)
    wave.set_data([series], 320, 100)
    assert wave.sample_at(series, 150) is None
    assert wave.sample_at(series, 300) == (300, 4)
    assert wave.sample_at(series, 22) == (20, 2)
    assert wave.sample_at(series, 321) is None
    assert list(times) == [0, 20, 40, 300, 320]
    assert list(values) == [1, 2, 3, 4, 5]
    wave.close()


def test_event_track_preserves_ids_selection_and_view(app):
    panel = SignalPanel()
    panel.resize(1000, 250)
    panel.show()
    ts = np.arange(0, 10001, 20)
    panel.set_data([PlotSeries("ax", "AX", "g", "#159c8d", ts, np.sin(ts / 1000))], 10000)
    labels = [{"name": "test", "color": "#159c8d"} for _ in range(17)]
    events = [{"id": 42, "li": 16, "t0": 1000, "t1": 2000}, {"id": 43, "li": 0, "t0": 5000, "t1": None}]
    panel.set_events(labels, events)
    panel.set_view(0, 10000)
    app.processEvents()
    panel.track.grab()
    selected = []
    panel.eventSelected.connect(selected.append)
    rect, identifier = next(x for x in panel.track.hits if x[1] == 42)
    QTest.mouseClick(panel.track, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert selected == [42] and panel.wave._selected_event_id == 42
    assert panel.focus_event(42) and panel.view_range == (0, 10000)
    panel.clear_data()
    assert panel.wave._events == []
    panel.close()


def test_many_events_scroll_without_squeezing_waveform(app):
    panel = SignalPanel()
    labels = [{"name": str(i), "color": "#159c8d"} for i in range(20)]
    events = [{"id": i, "li": i, "t0": i, "t1": i + 1} for i in range(20)]
    panel.set_events(labels, events)
    assert panel.track.height() == 20 * 26
    assert panel.scroll.height() <= 56
    assert len(panel.wave._events) == 20
    panel.close()
