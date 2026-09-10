"""Repeated demand updates must not restart the current clip at its first OCR frame."""
import threading
import time
from types import SimpleNamespace

from PIL import Image

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.worker import IndexWorker


def test_same_time_window_can_finish_despite_progress_and_position_refreshes(tmp_path, monkeypatch):
    import cowmata_tailring.workspace.worker as module

    (tmp_path / 'imou00162.mp4').write_bytes(b'isolated slow-inspection input')
    cat = Catalog(tmp_path, stability_seconds=0)
    cat.scan(fast=True)
    row = cat.rows()[0]
    cat.save_video_hint(row['path'], row['stamp'], {'start_ms': 1000, 'end_ms': 9000})
    worker = IndexWorker(cat)
    entered = threading.Event()

    class Inspector:
        def __init__(self, *_):
            self.timezone_minutes = 480

        def __call__(self, *_):
            entered.set()
            # GUI save/position updates do not change this record's clock range.
            worker.request('window', (1000, 9000, {'priority_reference_ms': 4000, 'review_progress': {'saved': 1}}))
            return {'needs_review': True, 'start_display': '待确认', 'intervals': []}

    monkeypatch.setattr(module, 'SourceInspector', Inspector)
    worker.request('window', (1000, 9000, {'priority_reference_ms': 2000}))
    worker.start()
    try:
        assert entered.wait(2)
        deadline = time.monotonic() + 1
        while cat.rows()[0]['state'] != 'review' and time.monotonic() < deadline:
            time.sleep(.01)
        assert cat.rows()[0]['state'] == 'review', 'same-window refresh repeatedly cancelled completed inspection'
        assert not worker.job_stop.is_set()
    finally:
        worker.cancel()
        worker.thread.join(3)
        cat.close()


def test_new_record_clock_or_explicit_pause_still_cancels_old_demand(tmp_path):
    cat = Catalog(tmp_path)
    worker = IndexWorker(cat)
    try:
        worker.request('window', (1000, 9000, {}))
        worker.job_stop.clear()
        worker.request('window', (11000, 19000, {}))
        assert worker.job_stop.is_set()
        worker.job_stop.clear()
        worker.request('pause')
        assert worker.job_stop.is_set()
        worker.request('window', (11000, 19000, {}))
        worker.job_stop.clear()
        worker.request('window', (11000, 19000, {'camera_maps': {'view01': {'anchors': [[0, 1000]]}}}))
        assert worker.job_stop.is_set(), 'changed camera clock must invalidate the old search'
    finally:
        cat.close()


def test_unreadable_video_has_bounded_ocr_and_does_not_cancel_next_file(tmp_path, monkeypatch):
    import cowmata_tailring.workspace.probe as module
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.ocr import TimestampOCR

    path = tmp_path / 'unreadable-opening.mp4'
    path.write_bytes(b'fixture with independently decoded timestamps below')
    inspector = module.SourceInspector(tmp_path, tmp_path)
    elapsed = [0.0]
    calls = [0]
    class Engine:
        def __call__(self, *_args, **_kwargs):
            if self.cancelled():
                raise InterruptedError('time budget or actual cancellation')
            calls[0] += 1
            elapsed[0] += 5
            return [], None
    ocr = TimestampOCR.__new__(TimestampOCR)
    ocr.engine, ocr.signature = Engine(), 'test'
    inspector.ocr = ocr
    frame = Image.new('RGB', (100, 100))
    timeline = MediaTimelineIndex('synthetic', 1, 0, 0, 100, (TimelineSegment(0, 10000, 0, 10000),), ())
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(module, 'probe_media', lambda *_a, **_kw: {'streams': [{'codec_type': 'video', 'width': 100, 'height': 100}]})
    monkeypatch.setattr(module, 'read_native_index', lambda *_a, **_kw: None)
    monkeypatch.setattr(module, 'find_ffmpeg', lambda: ('ffmpeg', 'ffprobe'))
    monkeypatch.setattr(module, 'probe_media_timeline', lambda *_a, **_kw: timeline)
    monkeypatch.setattr(module, 'extract_frame', lambda _p, t, *_a, **_kw: (frame, t))
    result = inspector.video(path, 'c'*64)
    assert result['needs_review'] and not result['intervals']
    assert calls[0] <= 15, 'unreadable clip exhausted dozens of enhancement calls instead of yielding'
    assert any('限时' in text for text in result['warnings'])
    assert not inspector.stop.is_set(), 'a time budget must not cancel the next file'
    assert not ocr.engine.cancelled()

    # The next source/position can still supply actual observations. The
    # exhausted call must not leave the engine's cancel hook stuck on True.
    def readable(_frame, **kwargs):
        target = float(kwargs['filename'].split('@')[1][:-2])
        return {'success': True, 'wall_ms': 100000 + target, 'roi': [0, .8, 1, 1], 'metadata': {}}
    ocr.recognize = readable
    good = inspector.video(path, 'd'*64)
    assert any(i['verified'] and i['wall_start'] == 100000 and i['wall_end'] == 101200 for i in good['intervals'])
    assert any(i['verified'] and i['wall_start'] == 101200 and i['wall_end'] == 109850 for i in good['intervals'])


def test_budget_does_not_swallow_explicit_user_cancellation(tmp_path):
    import pytest

    from cowmata_tailring.workspace.probe import SourceInspector

    inspector = SourceInspector(tmp_path, tmp_path)
    inspector.ocr = SimpleNamespace(engine=SimpleNamespace(cancelled=None),
        recognize=lambda *_a, **_kw: pytest.fail('cancelled OCR executed'))
    inspector.stop.set()
    with pytest.raises(InterruptedError):
        inspector._recognize_bounded(Image.new('RGB', (100, 100)), time.monotonic() + 12)


def test_pending_time_hover_explains_the_available_next_step(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QTableWidget

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    cat = Catalog(tmp_path)
    window.catalog = cat
    window.rows = [{'path': 'imou00162.mp4', 'kind': 'video', 'asset_id': 'a'*64, 'state': 'pending',
                    'error': '按需任务已切换，稍后可继续', 'metadata': {'start_display': '待确认'}}]
    def inspect(dialog):
        table = dialog.findChild(QTableWidget)
        text = table.item(0, 3).toolTip()
        assert '时间' in text and '重新建立所选视频索引' in text
        assert len(text) > len('待确认')
    monkeypatch.setattr(QDialog, 'exec', inspect)
    try:
        window.source_manager()
    finally:
        window.close()
        app.processEvents()
