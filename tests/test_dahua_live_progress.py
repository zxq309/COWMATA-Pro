import json
import sys
import time

import pytest


def test_ffmpeg_progress_delivered_before_child_exit(tmp_path):
    from cowmata_tailring.media import subprocess_tools
    assert hasattr(subprocess_tools, 'run_progress'), 'FFmpeg needs incremental progress reporting'
    marker = tmp_path / 'finished'
    code = "import time,pathlib;print('frame=25\\nout_time_us=1000000\\nspeed=2.0x\\nprogress=continue',flush=True);time.sleep(.3);pathlib.Path(" + repr(str(marker)) + ").write_text('done');print('frame=50\\nout_time_us=2000000\\nprogress=end',flush=True)"
    events = []
    def update(values):
        events.append((dict(values), marker.exists()))
    result = subprocess_tools.run_progress([sys.executable, '-c', code], progress=update, timeout=5)
    assert result.returncode == 0
    assert events[0][0]['frame'] == '25' and not events[0][1]
    assert events[-1][0]['frame'] == '50'
    assert b'frame=50' in result.stdout


def test_ffmpeg_stall_and_cancel_release_owned_process():
    from cowmata_tailring.media import subprocess_tools
    assert hasattr(subprocess_tools, 'run_progress'), 'FFmpeg needs a bounded stalled-helper policy'
    command = [sys.executable, '-c', 'import time;time.sleep(30)']
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='无进展'):
        subprocess_tools.run_progress(command, timeout=5, stall_timeout=.25)
    assert time.monotonic() - started < 4
    with pytest.raises(RuntimeError, match='取消'):
        subprocess_tools.run_progress(command, cancelled=lambda: True, timeout=5)


def test_live_stage_progress_keeps_timers_and_is_persisted(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import dahua_run
    now = [100.0]
    monkeypatch.setattr(dahua_run.time, 'monotonic', lambda: now[0])
    row = dict(id='one', source='original.dav', group='channel:1')
    log = dahua_run.RunLog(tmp_path, [row], {'channel:1': '视角01'}, lambda _: None)
    log.begin('one')
    now[0] += 1
    log.stage('convert', 'encoding', media_percent=25, frames=25, output_bytes=1048576)
    now[0] += 6
    log.pulse()
    stored = json.loads((tmp_path / 'dahua-run.json').read_text(encoding='utf-8'))
    assert stored['records'][0]['status'] == 'processing', 'Durable report is frozen at the last completed segment'
    assert stored['records'][0]['convert_seconds'] == 6
    log.stage('verify', 'checking')
    assert log.snapshot().get('media_percent', 0) == 0, 'New stage must not show the previous encoder percentage'
    assert log.snapshot()['convert_seconds'] == 6


def test_timing_sheet_shows_real_stage_progress_and_temporary_size():
    from cowmata_tailring.workspace.dahua_run_ui import DahuaRunTables
    table = DahuaRunTables()
    try:
        table.begin()
        table.accept(dict(event_kind='task_record', source_id='one', owner='视角01', source='raw', status='processing',
                          phase='verify', targets=[], media_percent=50.0, fps=200.0, media_speed='8x',
                          frames=1500, output_bytes=10485760, file_seconds=7, message='完整校验 50.0%'))
        table.flush()
        assert table.timing.columnCount() >= 10, 'Timing sheet has no current-stage column'
        assert '50.0%' in table.timing.item(0, 9).text()
        assert '暂存' in table.timing.item(0, 7).text()
        assert '50.0%' in table.live.item(0, 6).text()
    finally:
        table.close()


def test_qsv_decoder_failure_preserves_encoder_and_cleans_candidate(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import dahua_media as media
    target = tmp_path / 'final.mp4'
    monkeypatch.setattr(media, 'probe', lambda *a, **k: {'video': {'codec_name': 'hevc', 'pix_fmt': 'yuv420p', 'color_range': 'tv'}})
    calls, stages = [], []
    def attempt(source, output, *args, **kwargs):
        calls.append(kwargs.get('decoder'))
        if kwargs.get('decoder'):
            output.write_bytes(b'incomplete')
            raise ValueError('GPU decoder rejected this source')
        assert kwargs['encoder'] == 'h264_qsv'
        output.write_bytes(b'validated output')
        return {'settings': {'hardware_decoded': False}}
    monkeypatch.setattr(media, '_encode_attempt', attempt)
    result = media._encode('source.dav', target, 0, 1000, encoder='h264_qsv',
                           stage=lambda phase, message, **kw: stages.append(message))
    assert calls == ['hevc_qsv', None]
    assert target.read_bytes() == b'validated output'
    assert list(tmp_path.iterdir()) == [target]
    assert not result['settings']['hardware_decoded']
    assert any('回退软件解码' in s for s in stages)


def test_progress_never_reports_nan_or_previous_stage_bytes():
    from cowmata_tailring.workspace.dahua_media import media_progress
    events = []
    callback = media_progress(lambda phase, message, **kw: events.append(kw), 'verify', '校验', 1000)
    callback({'out_time_us': 'N/A', 'fps': 'nan', 'speed': 'N/A', 'frame': '0', 'total_size': '-1'})
    assert events == [dict(media_percent=0, frames=0, fps=0, media_speed='N/A')]


def test_full_range_recordings_keep_verified_software_color_conversion(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import dahua_media as media
    monkeypatch.setattr(media, 'probe', lambda *a, **k: {'video': {'codec_name': 'hevc', 'pix_fmt': 'yuvj420p', 'color_range': 'pc'}})
    calls = []
    def attempt(source, output, *args, **kwargs):
        calls.append(kwargs.get('decoder'))
        return {'info': {}}
    monkeypatch.setattr(media, '_encode_attempt', attempt)
    media._encode('full-range.dav', tmp_path / 'out.mp4', 0, 1000, encoder='h264_qsv')
    assert calls == [None], 'Full-range camera recordings must not use an unproved GPU range conversion'
