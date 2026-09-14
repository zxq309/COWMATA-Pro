import csv
import json
import time
from pathlib import Path

import pytest

from cowmata_tailring.workspace.classification_report import (
    CSV_FIELDS,
    LiveReport,
    counts,
    read_snapshot,
)


def test_current_csv_and_summary_count_each_source_once(tmp_path):
    with LiveReport(tmp_path) as report:
        report.seed([dict(source='G:/camera/a.mp4', status='pending'),
                     dict(source='G:/camera/b.mp4', status='pending')])
        report.row(dict(source='g:\\CAMERA\\a.mp4', status='processing'))
        report.row(dict(source='G:/camera/a.mp4', status='done', existing_verified=True))
        report.row(dict(source='G:/camera/b.mp4', status='empty_video'))
    snapshot = read_snapshot(tmp_path)
    rows = list(csv.DictReader(report.path.open(encoding='utf-8-sig', newline='')))
    assert list(rows[0]) == CSV_FIELDS and len(CSV_FIELDS) == 8
    assert [r['序号'] for r in rows] == ['1', '2']
    assert [r['状态'] for r in rows] == ['已复用', '无录像内容']
    assert snapshot['counts'] == dict(total=2, archived=1, reused=1,
                                     pending=0, processing=0, unavailable=1, errors=0)
    assert len(snapshot['rows']) == len(rows)


def test_pause_publishes_the_last_pending_changes(tmp_path):
    with pytest.raises(InterruptedError), LiveReport(tmp_path) as report:
        report.seed([dict(source='one.mp4', status='pending'),
                     dict(source='two.mp4', status='pending')])
        report.row(dict(source='one.mp4', status='done'))
        report.row(dict(source='two.mp4', status='processing'))
        raise InterruptedError('pause')
    snapshot = read_snapshot(tmp_path)
    assert snapshot['phase'] == 'paused'
    assert snapshot['counts']['archived'] == 1
    assert snapshot['counts']['pending'] == 1
    assert snapshot['counts']['processing'] == 0


def test_report_viewer_updates_while_open(qt_application, tmp_path):
    from cowmata_tailring.workspace.classification_viewer import ClassificationReportWindow
    report = LiveReport(tmp_path)
    report.seed([dict(source='one.mp4', status='pending')])
    viewer = ClassificationReportWindow(None, lambda: tmp_path)
    try:
        viewer.show()
        assert viewer.model.cells[0][1] == '待处理'
        report.row(dict(source='one.mp4', status='done'))
        report.flush(force=True)
        deadline = time.monotonic()+2.5
        while viewer.model.cells[0][1] != '已归类' and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(.01)
        assert viewer.model.cells[0][1] == '已归类'
        assert viewer.model.rowCount() == 1
        assert viewer.snapshot['counts'] == counts(viewer.model.rows)
    finally:
        viewer.timer.stop()
        viewer.close()


def test_blank_storage_slot_is_confirmed_without_ffmpeg(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v
    source = tmp_path/'blank.mp4'
    source.write_bytes(bytes(1024*1024))
    monkeypatch.setattr(v, 'probe', lambda *a: pytest.fail('Blank data reached ffprobe'))
    monkeypatch.setattr(v, 'opening_frame', lambda *a: pytest.fail('Blank data reached decoder'))
    row = v.inspect(source, tmp_path/'cache')
    assert row['status'] == 'empty_video'
    assert row['health']['checked_bytes'] == source.stat().st_size
    assert source.exists() and not row.get('target')


def test_zero_prefix_does_not_hide_data_elsewhere(tmp_path):
    from cowmata_tailring.workspace.intake_health import blank_recording
    source = tmp_path/'padded.mp4'
    source.write_bytes(bytes(256*1024)+b'actual data'+bytes(256*1024))
    assert blank_recording(source, tmp_path/'cache') is None


def test_changed_blank_file_does_not_reuse_a_stale_verdict(tmp_path):
    from cowmata_tailring.workspace.intake_health import blank_recording
    source = tmp_path/'slot.mp4'
    source.write_bytes(bytes(1024))
    assert blank_recording(source, tmp_path/'cache')['all_zero']
    source.write_bytes(b'valid data')
    assert blank_recording(source, tmp_path/'cache') is None


def test_empty_files_are_accounted_for_without_a_manual_error(tmp_path):
    from cowmata_tailring.workspace.video_intake import organize
    source = tmp_path/'input/zero.mp4'
    source.parent.mkdir()
    source.write_bytes(bytes(4096))
    result = organize(tmp_path/'farm', [dict(path=str(source), kind='video', camera='视角01')],
                      category='calving', farm=str(tmp_path/'farm'), cache=tmp_path/'cache', job=tmp_path/'job')
    assert result['completed'] and result['unresolved'] == 0 and result['archived_files'] == 0
    assert result['counts']['total'] == 1 and result['counts']['unavailable'] == 1
    assert result['rows'][0]['status'] == 'empty_video'
    assert not list((tmp_path/'farm').rglob('*.mp4'))
    state = json.loads((tmp_path/'job/report-state.json').read_text(encoding='utf-8'))
    assert state['counts'] == result['counts']


def test_inventory_includes_remaining_files_when_a_batch_is_paused(tmp_path, monkeypatch):
    from test_classifier_hotfix import video_row

    from cowmata_tailring.workspace import video_intake as v
    source = tmp_path/'input'
    source.mkdir()
    for name in ('a.mp4', 'b.mp4', 'c.mp4'):
        (source/name).write_bytes(name.encode())
    monkeypatch.setattr(v, 'inspect', video_row)
    stop = []
    def row_done(row):
        if row['status'] == 'done':
            stop.append(True)
    with pytest.raises(InterruptedError):
        v.organize(tmp_path/'farm', [dict(path=str(source), kind='video', camera='视角01')],
                   category='calving', farm=str(tmp_path/'farm'), cache=tmp_path/'cache',
                   job=tmp_path/'job', cancelled=lambda: bool(stop), on_row=row_done)
    state = read_snapshot(tmp_path/'job')
    assert state['counts']['total'] == 3
    assert state['counts']['archived'] == 1
    assert state['counts']['pending'] == 2
    csv_rows = list(csv.DictReader(Path(state['csv_path']).open(encoding='utf-8-sig')))
    assert len(csv_rows) == 3


def test_same_content_sources_reuse_one_destination_and_report_it(tmp_path, monkeypatch):
    from test_classifier_hotfix import video_row

    from cowmata_tailring.workspace import video_intake as v
    folder = tmp_path/'input'
    folder.mkdir()
    for name in ['a.mp4', 'b.mp4']:
        (folder/name).write_bytes(b'identical recording')
    monkeypatch.setattr(v, 'inspect', video_row)
    result = v.organize(tmp_path/'farm', [dict(path=str(folder), kind='video', camera='视角01')],
                        category='calving', farm=str(tmp_path/'farm'), cache=tmp_path/'cache', job=tmp_path/'job')
    assert result['copied'] == 1
    assert result['counts']['total'] == result['counts']['archived'] == 2
    assert result['counts']['reused'] == 1
    assert len(list((tmp_path/'farm').rglob('*.mp4'))) == 1
