"""Customer regressions: synchronized cursor, stable views and direct controls."""
from time import perf_counter
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoInterval, VideoTimeline
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.presentation import PresentationVideoBoard
from cowmata_tailring.workspace.work import SessionWork
from cowmata_tailring.workspace.worker import IndexWorker


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    w = MainWindow()
    w.confirm_close = lambda: 'discard'
    for timer in (w.board.timer, w.board.control_timer, w.board.decoder_timer, w.source_timer, w.save_timer):
        timer.stop()
    w.catalog = Catalog(tmp_path, stability_seconds=0)
    yield w
    w.close()
    app.processEvents()


def test_follow_cursor_crosses_real_sampling_gap_without_faking_samples(window):
    times = np.array([0., 20., 5000., 5020.])
    window.motion = SimpleNamespace(duration_ms=5020, times_ms=times,
        nearest_sample_index=lambda value: int(np.abs(times-value).argmin()))
    window.work = SessionWork('a'*64)
    window.work.clock = ClockMap([Anchor(0, 100000)])
    window.plot.set_data([PlotSeries('ax', 'acc', 'g', '#00aaff', times, np.zeros(4))], 5020)
    window.linked = True
    for value in (1000, 1800, 3200, 4700):
        window.video_time_changed(100000+value)
        assert window.imu_ms == value, 'follow cursor snapped to a distant sample and appeared frozen'
    np.testing.assert_array_equal(window.motion.times_ms, times)
    window.linked = False
    window.seek_imu(1000)
    assert window.imu_ms == 20, 'manual sample positioning must still snap to actual data'


def test_decoder_recovery_publishes_final_clock_in_same_tick(app, monkeypatch):
    board = PresentationVideoBoard()
    board.timer.stop()
    board.select(['A'])
    interval = VideoInterval('a', 'a.mp4', 'A', 100000, 110000, 0, 10000)
    board.timeline = VideoTimeline([interval])
    board.reference_ms, board.playing = 101000, True
    tile = board.tiles['A']
    tile.asset_id, tile.interval = 'a', interval
    tile.pending = {'asset':'a', 'generation':board.generation, 'phase':'seeking',
                    'baseline':0, 'target':1000, 'start':perf_counter(), 'cold':True}
    tile.engine = SimpleNamespace(get_time_ms=lambda:1262,
        stats=lambda:SimpleNamespace(displayed_pictures=2, decoded_video=4),
        video_output_count=lambda:1, set_rate=lambda *_:None, pause=lambda *_:None)
    monkeypatch.setattr(board, '_position', lambda *_:None)
    monkeypatch.setattr(board, '_prepare_next', lambda:None)
    clocks=[]
    board.timeChanged.connect(clocks.append)
    try:
        board.tick()
        assert tile.ready and not tile.pending
        assert clocks[-1] == board.reference_ms == 101262, 'IMU saw the old clock before decoder recovery'
    finally:
        tile.engine=None
        board.close()


def row(path, camera, start=None):
    return {'path': path, 'kind': 'video', 'asset_id': path if start is not None else None,
        'state': 'ready' if start is not None else 'pending', 'stamp': 'fixture', 'error': '',
        'metadata': {'camera': camera, 'intervals': [] if start is None else [
            {'wall_start': start, 'wall_end': start+10000, 'media_start': 0, 'media_end': 10000}]}}


def test_camera_list_survives_record_roundtrip_and_includes_unresolved_view(window, monkeypatch):
    monkeypatch.setattr(window, 'refresh_records', lambda: None)
    monkeypatch.setattr(window.board, '_request', lambda tile, interval, target, **kw: setattr(tile, 'asset_id', interval.asset_id))
    window.motion = SimpleNamespace(duration_ms=10000)
    window.work = SessionWork('a'*64)
    videos = [row('右1/one.mp4', '右1', 100000), row('左1/two.mp4', '左1', 300000), row('乐橙/imou.mp4', '乐橙')]
    window.rows = videos
    window.settings = {'selected_cameras': ['右1', '左1', '乐橙']}
    for base in (100000, 300000, 100000):
        window.work.clock = ClockMap([Anchor(0, base)])
        window.board.reference_ms = base+1000
        window.refresh_lists()
        names = [window.cameras.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window.cameras.count())]
        assert set(names) == {'右1', '左1', '乐橙'}, 'camera choices disappeared with the current record filter'
        assert set(window.board.selected) == set(names)
        # Discovery information must not create made-up video coverage.
        assert window.board.timeline.locate('乐橙', base+1000) is None
        assert all(r['metadata']['intervals'] for r in window.window_videos())


def test_generic_missing_camera_can_read_first_timestamp_while_main_plays(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        (tmp_path/'imou00162.mp4').write_bytes(b'generic recording fixture')
        cat.scan(fast=True)
        pending = cat.pending(eager=True)
        cat.save_video_hint(pending[0]['path'], pending[0]['stamp'], {'native_checked': True})
        worker = IndexWorker(cat)
        worker.window = (100000, 110000, {})
        worker.playback_busy.set()
        task = worker.next_task(pending)
        assert task and task[0] == 'hint'
        assert worker.may_run(task), 'non-native camera never gets its first timestamp while another view plays'
    finally:
        cat.close()


def test_pending_camera_selection_survives_first_identified_timestamp(window, monkeypatch):
    monkeypatch.setattr(window, 'refresh_records', lambda: None)
    monkeypatch.setattr(window.board, '_request', lambda tile, interval, target, **kw: setattr(tile, 'asset_id', interval.asset_id))
    window.motion = SimpleNamespace(duration_ms=10000)
    window.work = SessionWork('a'*64)
    window.work.clock = ClockMap([Anchor(0, 100000)])
    window.board.reference_ms = 101000
    window.settings = {'selected_cameras': ['右1', '乐橙']}
    window.rows = [row('右1/one.mp4', '右1', 100000), row('乐橙/imou1.mp4', None)]
    window.refresh_lists()
    assert set(window.board.selected) == {'右1', '乐橙'}
    identified = '乐橙 · 2560×1440 · top_right'
    window.rows[1] = row('乐橙/imou1.mp4', identified, 100000)
    window.rows.append(row('乐橙/imou2.mp4', None))
    window.refresh_lists()
    assert set(window.board.selected) == {'右1', identified}
    assert window.cameras.count() == 2, 'unread files must not duplicate their known camera'


def test_uncertain_camera_gap_does_not_claim_verified_absence(window, monkeypatch):
    monkeypatch.setattr(window, 'refresh_records', lambda: None)
    window.rows = [row('乐橙/imou1.mp4', '乐橙', 100000)]
    window.rows[0]['state'] = 'review'
    window.rows[0]['metadata']['needs_review'] = True
    window.board.reference_ms = 115000
    window.refresh_lists()
    assert '核验' in window.board.coverage_message('乐橙')
    assert window.board.timeline.locate('乐橙', 115000) is None


def test_deferred_search_cannot_block_another_missing_camera(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        paths = ['A/known.mp4', 'A/unknown.mp4', 'B/imou1.mp4', 'B/imou2.mp4', 'B/imou3.mp4']
        for path in paths:
            target = tmp_path/path
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(path.encode())
        cat.scan(fast=True)
        cat.index_one(paths[0], lambda *_: row(paths[0], 'A', 100000)['metadata'], eager=True)
        for pending in cat.pending(eager=True):
            cat.save_video_hint(pending['path'], pending['stamp'], {'native_checked': True})
        worker = IndexWorker(cat)
        worker.window = (100000, 110000, {})
        worker.playback_busy.set()
        task = worker.next_task(cat.pending(eager=True))
        assert task and task[1]['path'].startswith('B/') and worker.may_run(task)
        assert not worker.attempted, 'deferred sources must remain retryable after playback'
    finally:
        cat.close()


def test_aux_scroll_thumb_keeps_mouse_gesture_across_layout(app):
    board = PresentationVideoBoard()
    board.timer.stop()
    board.resize(1100, 450)
    board.select([f'view{i}' for i in range(8)])
    board.show()
    QTest.qWait(80)
    scroll = board.aux_scroll
    option = QStyleOptionSlider()
    scroll.initStyleOption(option)
    handle = scroll.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option, QStyle.SubControl.SC_ScrollBarSlider, scroll)
    try:
        QTest.mousePress(scroll, Qt.MouseButton.LeftButton, pos=handle.center())
        assert scroll.isSliderDown()
        QTest.mouseMove(scroll, handle.center()+QPoint(0, 45), delay=20)
        first = scroll.value()
        assert scroll.isSliderDown(), 'relayout hides scrollbar and cancels the drag'
        QTest.mouseMove(scroll, handle.center()+QPoint(0, 100), delay=20)
        assert scroll.value() > first
        QTest.mouseRelease(scroll, Qt.MouseButton.LeftButton, pos=handle.center()+QPoint(0, 100))
    finally:
        board.close()


def test_empty_preview_does_not_overwrite_coverage_with_click_to_play(app):
    board = PresentationVideoBoard()
    board.timer.stop()
    board.select(['A', 'B'])
    board.playback_policy, board.playing = 'focus', True
    tile = board.tiles['B']
    board.camera_discovery = {'B': 'pending'}
    try:
        board._preview_position('B', tile)
        before = tile.message.text()
        board._observe(tile, 100)
        assert tile.message.text() == before and '未索引' in before
    finally:
        board.close()


def test_frozen_preview_recovers_after_gap_and_next_clip(app):
    board = PresentationVideoBoard()
    board.timer.stop()
    board.select(['A', 'B'])
    board.playback_policy, board.playing = 'focus', True
    first = VideoInterval('old', 'first.mp4', 'B', 100000, 110000, 0, 10000)
    second = VideoInterval('new', 'second.mp4', 'B', 120000, 130000, 0, 10000)
    board.timeline = VideoTimeline([first, second])
    tile = board.tiles['B']
    tile.asset_id, tile.interval, tile.actual_ms, tile._preview_only = 'old', first, 101000, True
    board.frozen_previews.add((board.generation, 'B', 'old'))
    try:
        board.reference_ms = 115000
        board._preview_position('B', tile)
        assert tile.interval is None
        board.reference_ms = 121000
        board._preview_position('B', tile)
        assert tile.interval == second and tile.asset_id == 'new'
        assert tile.actual_ms is None, 'old clip snapshot must not stand in for the next clip'
    finally:
        board.close()


def test_click_paused_preview_message_promotes_and_plays_camera(app, monkeypatch):
    board = PresentationVideoBoard()
    board.timer.stop()
    board.select(['A', 'B'])
    board.resize(1000, 450)
    board.show()
    board.playback_policy, board.playing = 'focus', True
    board.reference_ms = 101000
    intervals = [VideoInterval(c, c+'.mp4', c, 100000, 110000, 0, 10000) for c in ('A', 'B')]
    board.timeline = VideoTimeline(intervals)
    monkeypatch.setattr(board, '_request', lambda tile, interval, target, **kw: setattr(tile, 'asset_id', interval.asset_id))
    monkeypatch.setattr(board, '_preview_position', lambda *_: None)
    tile = board.tiles['B']
    tile._preview_only = True
    tile.interval = intervals[1]
    tile.status('已暂停，点击播放')
    QTest.qWait(80)
    try:
        QTest.mouseClick(tile.message, Qt.MouseButton.LeftButton)
        assert board.main_camera == 'B' and board.playing
        assert not board.is_preview('B')
    finally:
        board.close()
