"""Offline integration tests use a real local HTTP server and temporary farms."""
import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from cowmata_tailring.edge_download.core import (
    CHINA,
    DownloadError,
    Job,
    Target,
    run_job,
    save_record,
    validate_payload,
)

START = datetime(2026, 8, 17, tzinfo=CHINA)
RAW = bytes(4) + b'\x00\x01' * 9 + (20).to_bytes(4, 'little') + b'\x00\x01' * 9


def record(kind='motion', stamp=None, cow='23077', device='546C50CA07FA'):
    data = dict(device=device, cow_id=cow, create_time=int((stamp or START).timestamp() * 1000), version=2)
    if kind == 'motion':
        data['imu'] = base64.b64encode(RAW).decode()
    else:
        data['data'] = base64.b64encode(b'\x00\x01' * 6).decode()
        data['ir_data'] = base64.b64encode(b'\x01\x02' * 6).decode()
        data['imu_data'] = base64.b64encode(bytes(12)).decode()
    return data


@pytest.fixture
def server():
    state = dict(rows={1: record()}, pulse={2: record('pulse')}, requests=[], failure=False, blob=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            state['requests'].append((parsed.path, query))
            if parsed.path == '/blob':
                self.send_response(200)
                self.send_header('Content-Length', str(len(RAW)))
                self.end_headers()
                self.wfile.write(RAW)
                return
            if parsed.path == '/device/data/count':
                start = datetime.fromisoformat(query['startTime']).replace(tzinfo=__import__('datetime').timezone.utc)
                end = datetime.fromisoformat(query['endTime']).replace(tzinfo=__import__('datetime').timezone.utc)
                def included(r):
                    return start.timestamp() * 1000 <= r['create_time'] < end.timestamp() * 1000
                data = dict(motionRecords=[dict(uid=k, device=r['device'], cow_id=r['cow_id'])
                                          for k, r in state['rows'].items() if included(r)],
                            pulseUids=[k for k, r in state['pulse'].items() if included(r)])
            else:
                kind = 'pulse' if parsed.path.endswith('/pulse') else 'rows'
                data = dict(state[kind][int(query['uid'])])
                if state['failure']:
                    data['device'] = 'WRONG'
                if state['blob'] and kind == 'rows':
                    data['imu'] = None
                    data['url'] = f'http://127.0.0.1:{self.server.server_port}/blob'
                    data['_integrity'] = dict(imu_size=len(RAW), imu_sha256=hashlib.sha256(RAW).hexdigest(),
                                              frame_bytes=22, frame_count=2)
            raw = json.dumps(dict(code=0, data=data)).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}', state
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def job_for(root, url='http://127.0.0.1:18031', kinds=('motion', 'pulse')):
    return Job(url, root, '产犊', (Target('546C50CA07FA', '23077', 'E'),), kinds, START, START + timedelta(days=1))


def test_http_download_both_types_and_deduplicate(tmp_path, server):
    url, state = server
    job = job_for(tmp_path, url)
    messages = []
    first = run_job(job, threading.Event(), messages.append)
    assert (first.saved, first.failed) == (2, 0)
    assert (tmp_path / '产犊/Motion/2026-08-17/546C50CA07FA-23077-E/2026-08-17_00-00-00.json').is_file()
    assert (tmp_path / '产犊/PPG/2026-08-17/546C50CA07FA-23077-E/2026-08-17_00-00-00.json').is_file()
    details = sum(path != '/device/data/count' for path, _ in state['requests'])
    again = run_job(job, threading.Event())
    assert (again.saved, again.skipped, again.failed) == (0, 2, 0)
    assert sum(path != '/device/data/count' for path, _ in state['requests']) == details
    assert state['requests'][0][1]['startTime'] == '2026-08-16T16:00:00'


def test_binary_integrity_and_unchanged_payload(tmp_path, server):
    url, state = server
    state['blob'] = True
    result = run_job(job_for(tmp_path, url, ('motion',)), threading.Event())
    assert result.saved == 1 and result.failed == 0
    saved = json.loads(next(tmp_path.rglob('*.json')).read_bytes())
    assert 'url' not in saved
    assert base64.b64decode(saved['imu']) == RAW


def test_failed_records_retry_and_late_upload_is_discovered(tmp_path, server):
    url, state = server
    job = job_for(tmp_path, url, ('motion',))
    state['failure'] = True
    assert run_job(job, threading.Event()).failed == 1
    assert not list(tmp_path.rglob('*.json'))
    state['failure'] = False
    assert run_job(job, threading.Event()).saved == 1
    state['rows'][3] = record(stamp=START + timedelta(hours=1))
    result = run_job(job, threading.Event())
    assert (result.saved, result.skipped) == (1, 1)
    file = next(tmp_path.rglob('*01-00-00.json'))
    file.write_text('corrupt')
    result = run_job(job, threading.Event())
    assert result.saved == 1
    assert json.loads(file.read_bytes())['imu'] == state['rows'][3]['imu']
    assert any(p.read_bytes() == b'corrupt' for p in (tmp_path/'.edge-download/recovery').iterdir())


def test_cross_device_cow_query_preserves_history(tmp_path, server):
    url, state = server
    state['rows'][3] = record(device='0C3D5EA22DE3', stamp=START + timedelta(hours=1))
    job = replace(job_for(tmp_path, url, ('motion',)), targets=(Target(cow='23077'),))
    result = run_job(job, threading.Event())
    assert result.saved == 2
    assert any(p.name == '0C3D5EA22DE3-23077-待核对' for p in tmp_path.rglob('*'))
    assert state['requests'][0][1]['cow'] == '23077'


def test_history_mismatch_is_not_written(tmp_path, server):
    url, _ = server
    job = replace(job_for(tmp_path, url, ('motion',)), targets=(Target('546C50CA07FA', 'OTHER', 'E'),))
    result = run_job(job, threading.Event())
    assert result.failed == 1 and result.saved == 0


def test_reuse_same_day_mark_across_streams_and_keep_existing(tmp_path):
    job = job_for(tmp_path)
    target = Target('546C50CA07FA', '23077')
    folder = tmp_path / '产犊/Motion/2026-08-17/546C50CA07FA-23077-E'
    folder.mkdir(parents=True)
    old = folder / '2026-08-17_00-00-00.json'
    old.write_text(json.dumps(record()))
    result, saved = save_record(job, target, 'motion', record(), threading.Event())
    assert result == old and not saved
    path, saved = save_record(job, target, 'pulse', record('pulse'), threading.Event())
    assert saved and path.parent.name.endswith('-E')
    other = tmp_path / '产犊/PPG/2026-08-17/546C50CA07FA-23077-X'
    other.mkdir()
    with pytest.raises(DownloadError, match='多个现场记号'):
        save_record(job, target, 'pulse', record('pulse'), threading.Event())


def test_same_second_different_payload_no_overwrite(tmp_path):
    job = job_for(tmp_path)
    a = record()
    b = record()
    b['imu'] = base64.b64encode(bytes(44)).decode()
    first, _ = save_record(job, job.targets[0], 'motion', a, threading.Event())
    with pytest.raises(DownloadError, match='冲突'):
        save_record(job, job.targets[0], 'motion', b, threading.Event())
    assert list(first.parent.glob('*.json')) == [first]
    assert json.loads(first.read_bytes()) == a


@pytest.mark.parametrize('change', [dict(imu='???'), dict(version=8), dict(create_time=0),
                                     dict(_integrity={'imu_sha256': 'bad'})])
def test_reject_corrupt_records(change):
    with pytest.raises(DownloadError):
        validate_payload(record() | change, 'motion')


def test_path_escape_and_invalid_targets(tmp_path):
    with pytest.raises(DownloadError):
        replace(job_for(tmp_path), category='../other').validate()
    with pytest.raises(DownloadError):
        Target('../evil', 'cow').validate()
    with pytest.raises(DownloadError):
        replace(job_for(tmp_path), targets=(Target(cow='23077'),)).validate()


def test_day_boundary_and_farm_isolation(tmp_path):
    farm1, farm2 = tmp_path / 'farm1', tmp_path / 'farm2'
    farm1.mkdir()
    farm2.mkdir()
    for farm in (farm1, farm2):
        job = replace(job_for(farm), category='怀孕/孕晚期')
        data = record(stamp=START + timedelta(days=1))
        path, _ = save_record(job, job.targets[0], 'motion', data, threading.Event())
        assert path.is_relative_to(farm) and '2026-08-18' in str(path)


def test_pre_cancel_writes_no_data(tmp_path, server):
    cancel = threading.Event()
    cancel.set()
    result = run_job(job_for(tmp_path, server[0]), cancel)
    assert result.canceled and not list(tmp_path.rglob('*.json'))


@pytest.fixture(scope='module')
def qt_app():
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def dispose_worker(worker):
    from PySide6.QtCore import QCoreApplication, QEvent
    assert worker.wait(5000)
    worker.deleteLater()
    QCoreApplication.sendPostedEvents(worker, QEvent.Type.DeferredDelete)


def spin(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Qt operation timed out')


def test_manual_automatic_scheduled_workers(qt_app, tmp_path, server):
    from cowmata_tailring.edge_download.dialog import Worker
    job = job_for(tmp_path, server[0], ('motion',))
    for mode in ('手动', '自动', '定时'):
        cycles = []
        worker = Worker(job, mode, 0.05, datetime.now(CHINA) + timedelta(seconds=0.12))
        worker.cycle_done.connect(cycles.append)
        # Keep the auto fixture range small, but spanning the test records.
        if mode == '自动':
            from unittest.mock import patch
            with patch('cowmata_tailring.edge_download.dialog.datetime') as mock_date:
                mock_date.now.return_value = START + timedelta(hours=2)
                worker.start()
                spin(qt_app, lambda: len(cycles) >= 2)
                worker.cancel.set()
                assert worker.wait(5000)
        else:
            worker.start()
            spin(qt_app, lambda: not worker.isRunning())
        qt_app.processEvents()
        assert cycles and all(c.failed == 0 for c in cycles)
        dispose_worker(worker)


def test_scheduled_cancel_before_network(qt_app, tmp_path, server):
    from cowmata_tailring.edge_download.dialog import Worker
    worker = Worker(job_for(tmp_path, server[0]), '定时', 60, datetime.now(CHINA) + timedelta(hours=1))
    worker.start()
    worker.cancel.set()
    assert worker.wait(2000)
    assert not server[1]['requests']
    dispose_worker(worker)


def test_dialog_profiles_and_idempotent_menu(qt_app, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMainWindow

    from cowmata_tailring.edge_download import install_menu
    from cowmata_tailring.edge_download.dialog import DownloadDialog
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    settings.setValue('farms', json.dumps([{'root': str(tmp_path / 'a')}, {'root': str(tmp_path / 'b')}]))
    dialog = DownloadDialog(settings=settings)
    dialog.targets.item(0, 0).setText('DEVICE_A')
    dialog.farms.setCurrentIndex(1)
    assert dialog.target_values() == []
    dialog.targets.item(0, 0).setText('DEVICE_B')
    dialog.farms.setCurrentIndex(0)
    assert dialog.target_values()[0][0] == 'DEVICE_A'
    dialog.save_settings()
    dialog.close()
    host = QMainWindow()
    menu = host.menuBar().addMenu('工具')
    first = install_menu(host, menu)
    assert first is install_menu(host, menu)
    assert len(menu.actions()) == 1 and menu.actions()[0].text() == '端侧数据下载…'
    host.close()


def test_host_close_waits_for_worker(qt_app, tmp_path, server):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMainWindow

    from cowmata_tailring.edge_download import install_menu
    from cowmata_tailring.edge_download.dialog import DownloadDialog, Worker
    host = QMainWindow()
    controller = install_menu(host, host.menuBar().addMenu('工具'))
    controller.dialog = DownloadDialog(host, QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    dialog = controller.dialog
    dialog.worker = Worker(job_for(tmp_path, server[0]), '定时', 60, datetime.now(CHINA) + timedelta(hours=1), parent=dialog)
    dialog.worker.finished.connect(dialog.task_finished)
    dialog.worker.start()
    host.show()
    host.close()
    spin(qt_app, lambda: not host.isVisible() and not dialog.running)
    assert not server[1]['requests']


def test_real_annotator_menu_entry(qt_app):
    from cowmata_tailring.workspace.modern_window import MainWindow
    host = MainWindow()
    try:
        menus = [a.menu() for a in host.menuBar().actions() if a.menu()]
        tools = next(m for m in menus if m.title() == '数据准备(&T)')
        assert sum(a.objectName() == 'edgeDataDownloadAction' for a in tools.actions()) == 1
        assert host._edge_download_integration.dialog is None  # no network or window on startup
    finally:
        host.close()
        qt_app.processEvents()


def test_auto_recovers_after_cycle_failure(qt_app, tmp_path, monkeypatch):
    from cowmata_tailring.edge_download import dialog
    from cowmata_tailring.edge_download.core import Result
    calls = []
    def run(*args):
        calls.append(args)
        if len(calls) == 1:
            raise DownloadError('temporary server failure')
        args[1].set()
        return Result(saved=1)
    monkeypatch.setattr(dialog, 'run_job', run)
    worker = dialog.Worker(job_for(tmp_path), '自动', 0.01, START)
    worker.start()
    spin(qt_app, lambda: not worker.isRunning())
    assert len(calls) == 2
    dispose_worker(worker)


def test_dialog_starts_real_http_download(qt_app, tmp_path, server):
    from PySide6.QtCore import QDateTime, QSettings

    from cowmata_tailring.edge_download.dialog import DownloadDialog
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    settings.setValue('farms', json.dumps([dict(root=str(tmp_path), server=server[0],
                                              targets=[['546C50CA07FA', '23077', 'E']])]))
    dialog = DownloadDialog(settings=settings)
    dialog.start_at.setDateTime(QDateTime.fromString('2026-08-17 00:00:00', 'yyyy-MM-dd HH:mm:ss'))
    dialog.end_at.setDateTime(QDateTime.fromString('2026-08-18 00:00:00', 'yyyy-MM-dd HH:mm:ss'))
    dialog.start_button.click()
    assert not dialog.inputs.isEnabled()
    spin(qt_app, lambda: dialog.worker is None)
    assert dialog.inputs.isEnabled() and '本轮下载 1' in dialog.status.text()
    assert list(tmp_path.rglob('*.json'))
    dialog.close()
