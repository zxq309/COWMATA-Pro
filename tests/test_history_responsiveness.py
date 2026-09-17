import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.playback import VideoTile
from cowmata_tailring.workspace.presentation import PresentationVideoBoard


@pytest.fixture
def board():
    app = QApplication.instance() or QApplication([])
    widget = PresentationVideoBoard()
    widget.timer.stop()
    widget.decoder_timer.stop()
    widget.resize(1100, 600)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


def test_showing_eight_native_tiles_yields_to_input(board, monkeypatch):
    # Reproduce the measured cost of native QWidget.show without GPU variance.
    original = VideoTile.show
    shown = []
    beats = []
    pulse = QTimer()
    pulse.timeout.connect(lambda: beats.append(len(shown)))
    pulse.start(10)

    def slow_show(tile):
        if tile.isHidden():
            time.sleep(.06)
            shown.append(tile.camera)
        return original(tile)

    monkeypatch.setattr(VideoTile, "show", slow_show)
    began = time.perf_counter()
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    elapsed = time.perf_counter() - began
    QTest.qWait(900)
    pulse.stop()
    assert elapsed < .15, f"one input callback blocked for {elapsed:.3f}s"
    assert len(shown) == 8
    assert any(0 < count < 8 for count in beats), "input loop never ran while tiles were being shown"
    assert all(tile.isVisible() for tile in board.tiles.values())


def test_changed_layout_cannot_show_stale_queued_auxiliaries(board):
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    board.set_presentation("C")
    board.set_main("CAM05")
    QTest.qWait(180)
    assert board.tiles["CAM05"].isVisible()
    assert all(not tile.isVisible() for camera, tile in board.tiles.items() if camera != "CAM05")
    assert len(board.pool) == 8


def test_closing_board_discards_pending_native_shows(board):
    board.select([f"CAM{i:02d}" for i in range(1, 9)])
    board.close()
    QTest.qWait(120)
    assert board._closing
    assert not board.isVisible()

@pytest.mark.parametrize("with_clock", [True, False])
def test_history_waveform_and_position_use_saved_clock_without_changing_samples(tmp_path, with_clock):
    from cowmata_tailring.annotation.data import _synthetic_object, load_motion_json
    from cowmata_tailring.workspace.catalog import digest_file
    from cowmata_tailring.workspace.clocks import Anchor, ClockMap, wall_ms
    from cowmata_tailring.workspace.history_window import HistoryWindow
    from cowmata_tailring.workspace.label_file import build_label_file, save_label_file
    from cowmata_tailring.workspace.storage import atomic_json
    from cowmata_tailring.workspace.work import SessionWork

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.json"
    atomic_json(source, _synthetic_object(2))
    motion = load_motion_json(source)
    work = SessionWork(digest_file(source))
    if with_clock:
        work.clock = ClockMap([Anchor(0, wall_ms("2026-09-10 12:34:56"))])
    work.project.add_event(0, 20, 40)
    document = build_label_file(work, motion, tmp_path, [], {}, selection=(20, 40))
    document["source"]["project_root_hint"] = ""
    path = tmp_path / "history.annotations.json"
    save_label_file(path, document)
    before = digest_file(path)
    viewer = HistoryWindow(path)
    viewer.future.result(timeout=5)
    viewer.poll_load()
    if not with_clock:
        # Exercise the display's missing-clock contract directly; current full
        # records can legitimately recover an origin from their capture metadata.
        viewer.data.work.clock = work.clock
        viewer.apply_data(viewer.data)
    try:
        assert viewer.data.motion.times_ms.tolist() == [20, 40]
        if with_clock:
            assert viewer.plot.wave._format_time(20, True) == "2026-09-10 12:34:56.020"
            assert "2026-09-10 12:34:56.020" in viewer.position.text()
            assert "2026-09-10 12:34:56.020" in viewer.events.item(0).text()
            assert "2026-09-10 12:34:56.040" in viewer.events.item(0).text()
        else:
            assert viewer.plot.wave.clock is None
            assert "1970" not in viewer.position.text()
            assert "未校准" in viewer.position.text()
            assert "相对 0.020 秒" in viewer.events.item(viewer.events.count() - 1).text()
            assert "相对 0.040 秒" in viewer.events.item(viewer.events.count() - 1).text()
        assert [(event.t0, event.t1) for event in viewer.data.work.project.events] == [(20, 40)]
        assert viewer.data.work.clock.to_dict() == work.clock.to_dict()
        assert digest_file(path) == before
    finally:
        viewer.dispose()
        app.processEvents()
