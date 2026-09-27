"""4.3.1: operators must read cow marks in the video while locking waveform intervals."""
import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.modern_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    w.board.timer.stop()
    w.save_timer.stop()
    w.source_timer.stop()
    w.resize(1600, 1000)
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


def handles(window):
    return {c: int(t.surface.winId()) for c, t in window.board.tiles.items()}


def test_enlarge_video_spans_stage_width_and_keeps_waveform_strip(window, app):
    window.board.select([f"camera-{i}" for i in range(4)])
    window.set_presentation("C")
    app.processEvents()
    stage = window.stage
    pip_area = stage.video.width() * stage.video.height()
    before = handles(window), window.board.generation
    window.enlarge_video()
    app.processEvents()
    assert stage.video_focus and stage.focus_button.isChecked()
    assert stage.video.width() == stage.width()
    assert stage.video.width() * stage.video.height() > 2 * pip_area
    wave = stage.signal_panel.geometry()
    assert wave.height() >= 200 and wave.width() == stage.width()
    assert not wave.intersects(stage.video.geometry())
    assert stage.rect().contains(stage.video.geometry()) and stage.rect().contains(wave)
    assert (handles(window), window.board.generation) == before  # no reload / reparent
    # PiP dragging is meaningless while enlarged and must not move the video.
    geometry = stage.video.geometry()
    assert stage.drag_header.isVisible() and not stage.pip_active()
    stage.toggle_video_focus()
    app.processEvents()
    assert not stage.video_focus and stage.pip_active()
    assert stage.video.geometry() != geometry


def test_enlarge_from_other_modes_switches_to_waveform_layout(window, app):
    window.set_presentation("A")
    window.enlarge_video()
    app.processEvents()
    assert window.stage.mode == "C" and window.stage.video_focus


def test_waveform_window_gives_video_the_whole_stage_and_docks_back(window, app):
    window.board.select([f"camera-{i}" for i in range(4)])
    window.set_presentation("C")
    app.processEvents()
    stage, panel = window.stage, window.stage.signal_panel
    before = handles(window), window.board.generation
    window.toggle_waveform_window()
    app.processEvents()
    assert stage.wave_window is not None and stage.wave_window.isVisible()
    assert panel.window() is stage.wave_window and panel.isVisible()
    assert stage.video.geometry() == stage.rect()
    assert stage.split_button.isChecked()
    assert (handles(window), window.board.generation) == before
    # Labelling/playback hotkeys still work while the waveform window has focus.
    keys = {sc.key().toString() for sc in stage.wave_window.mirrors if hasattr(sc, "activated")}
    assert {"Space", "[", "]", "Left", "Right"} <= keys
    from PySide6.QtGui import QShortcut
    original = next(sc for sc in window.findChildren(QShortcut)
                    if sc.key().toString() == "]" and sc.parent() is window)
    calls = []
    original.activated.connect(lambda: calls.append("step"))
    proxy = next(sc for sc in stage.wave_window.mirrors
                 if hasattr(sc, "activated") and sc.key().toString() == "]")
    proxy.activated.emit()
    assert calls == ["step"]
    before = handles(window), window.board.generation  # the step itself seeks
    actions = {a.shortcut().toString() for a in stage.wave_window.actions()}
    assert "Ctrl+S" in actions and "Ctrl+Shift+E" in actions
    # Closing the waveform window re-docks it in the main stage.
    stage.wave_window.close()
    app.processEvents()
    app.processEvents()
    assert stage.wave_window is None
    assert panel.parentWidget() is stage and panel.isVisible()
    assert stage.rect().contains(panel.geometry()) and panel.height() > 200
    assert (handles(window), window.board.generation) == before


def test_waveform_window_mirrors_only_main_window_hotkeys(window, app):
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QDialog

    other = QDialog(window)
    QShortcut(QKeySequence("Ctrl+Alt+Q"), other)
    window.toggle_waveform_window()
    app.processEvents()
    keys = {sc.key().toString() for sc in window.stage.wave_window.mirrors if hasattr(sc, "activated")}
    assert "Ctrl+Alt+Q" not in keys
    window.toggle_waveform_window()
    app.processEvents()
    assert window.stage.wave_window is None


def test_main_window_close_redocks_waveform(app):
    w = MainWindow()
    w.board.timer.stop()
    w.save_timer.stop()
    w.source_timer.stop()
    w.show()
    app.processEvents()
    w.toggle_waveform_window()
    app.processEvents()
    panel = w.stage.signal_panel
    w.close()
    app.processEvents()
    assert w.stage.wave_window is None and panel.parentWidget() is w.stage