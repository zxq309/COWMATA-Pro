import json
import threading
import time
from PySide6.QtWidgets import QApplication
from pathlib import Path

import pytest
from test_classifier_hotfix import video_row
from test_fixes_v351 import organizer  # noqa: F401


def wait_for_views(window):
    deadline = time.monotonic() + 5
    while window._discovering and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.01)
    assert not window._discovering


@pytest.mark.parametrize('transfer', ['copy', 'move'])
def test_selected_views_transfer_at_the_same_time(tmp_path, monkeypatch, transfer):
    from cowmata_tailring.workspace import fast_transfer, resource_import
    from cowmata_tailring.workspace import organization as core
    from cowmata_tailring.workspace import video_intake as v
    monkeypatch.setattr(v, 'inspect', video_row)
    sources = []
    for i in range(3):
        p = tmp_path / f'camera{i}' / 'a.mp4'
        p.parent.mkdir()
        p.write_bytes(f'camera{i}'.encode() * 100)
        sources.append(dict(kind='video', path=str(p.parent), camera=f'视角0{i+1}'))
    plan = v.plan_import(tmp_path / 'farm', sources, category='calving', farm=str(tmp_path / 'farm'), cache=tmp_path / 'cache', transfer=transfer)
    barrier = threading.Barrier(3)
    copy, move = fast_transfer.copy_verified, core.move_no_replace
    def concurrent_copy(*args, **kwargs):
        barrier.wait(timeout=3)
        return copy(*args, **kwargs)
    def concurrent_move(src, dst):
        if Path(src).suffix == '.mp4' and Path(src).parent.name.startswith('camera'):
            barrier.wait(timeout=3)
        return move(src, dst)
    monkeypatch.setattr(fast_transfer, 'copy_verified', concurrent_copy)
    monkeypatch.setattr(core, 'move_no_replace', concurrent_move)
    result = resource_import.execute(plan, tmp_path / 'job')
    assert result['moved'] == 3
    records = json.loads((Path(plan['target']) / '资源索引.json').read_text(encoding='utf-8'))['records']
    assert len(records) == 3
    for row in plan['rows']:
        assert Path(row['target']).read_bytes() == Path(row['source']).parent.name.encode() * 100
        assert Path(row['source']).exists() == (transfer == 'copy')

def test_parent_directory_lists_views_and_only_checked_views_are_sources(organizer, tmp_path):  # noqa: F811
    from PySide6.QtCore import Qt
    for name in ('乐橙', '右1', '右2'):
        p = tmp_path / name / 'day' / 'a.mp4'
        p.parent.mkdir(parents=True)
        p.write_bytes(b'camera recording')
    discover = getattr(organizer, 'set_video_root', None)
    assert callable(discover), 'Missing parent-directory discovery with view selection'
    discover(tmp_path)
    wait_for_views(organizer)
    assert organizer.sources.rowCount() == 3
    assert organizer.source_specs() == []
    organizer.sources.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert len(organizer.source_specs()) == 1

def test_confirmed_zero_video_deleted_and_record_survives_resume(tmp_path):
    from cowmata_tailring.workspace import video_intake as v
    source = tmp_path / 'camera/a.mp4'
    source.parent.mkdir()
    source.write_bytes(bytes(1024))
    options = dict(delete_unusable=True, category='calving', farm=str(tmp_path / 'farm'), job=tmp_path / 'job', cache=tmp_path / 'cache')
    specs = [dict(kind='video', path=str(source.parent), camera='视角01')]
    result = v.organize(tmp_path / 'farm', specs, **options)
    assert not source.exists(), 'Confirmed blank recording was retained'
    assert result['rows'][0]['status'] == 'deleted'
    resumed = v.organize(tmp_path / 'farm', specs, **options)
    assert resumed['total_files'] == 1
    assert resumed['rows'][0]['status'] == 'deleted'

@pytest.mark.parametrize('picture,deleted', [('black', True), ('0x202020', False), ('testsrc', False), ('transient', False), ('last_bright', False)])
def test_full_video_health_distinguishes_black_from_dark_and_transient(tmp_path, picture, deleted):
    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
    from cowmata_tailring.media.subprocess_tools import run_cancellable
    from cowmata_tailring.workspace import intake_health
    path = tmp_path / 'sample.mp4'
    source = 'testsrc=size=64x64:rate=10:duration=1' if picture == 'testsrc' else 'color=c='+('black' if picture in {'transient', 'last_bright'} else picture)+':s=64x64:r=10:d=1'
    args = [str(find_ffmpeg()[0]), '-v', 'error', '-f', 'lavfi', '-i', source]
    if picture in {'transient', 'last_bright'}:
        args += ['-vf', "drawbox=x=0:y=0:w=64:h=64:color=white:t=fill:enable='gte(n," + ('9' if picture == 'last_bright' else '5') + ")'"]
    made = run_cancellable(args + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
    assert made.returncode == 0, made.stderr.decode(errors='replace')
    assess = getattr(intake_health, 'assess_video', None)
    assert callable(assess), 'Whole-video health verification is missing'
    result = assess(path, tmp_path / 'cache')
    assert bool(result.get('delete_reason')) == deleted
    assert path.exists(), 'Health inspection itself must remain read-only'


def test_decoder_timeout_never_authorizes_deletion(tmp_path, monkeypatch):
    import subprocess

    from cowmata_tailring.media import subprocess_tools
    from cowmata_tailring.workspace import intake_health
    path = tmp_path / 'sample.mp4'
    path.write_bytes(b'not a zero file')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('decoder', 2)
    monkeypatch.setattr(subprocess_tools, 'run_cancellable', timeout)
    assess = getattr(intake_health, 'assess_video', None)
    assert callable(assess)
    with pytest.raises(subprocess.TimeoutExpired):
        assess(path, tmp_path / 'cache')
    assert path.exists()


def test_damaged_selected_video_deleted_but_unselected_remains(tmp_path):
    from cowmata_tailring.workspace import video_intake as v
    sources = []
    for view in ('selected', 'unchecked'):
        path = tmp_path / view / 'broken.mp4'
        path.parent.mkdir()
        path.write_bytes(b'\x00\x00\x00\x18ftypmp42' + bytes(52))
        sources.append(path)
    result = v.organize(tmp_path / 'farm', [dict(kind='video', path=str(sources[0].parent), camera='视角01')],
        category='calving', farm=str(tmp_path / 'farm'), job=tmp_path / 'job', cache=tmp_path / 'cache', delete_unusable=True)
    assert not sources[0].exists()
    assert sources[1].exists()
    assert result['counts']['deleted'] == 1 and result['counts']['errors'] == 0


def test_new_selection_does_not_replay_old_unchecked_views(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v
    monkeypatch.setattr(v, 'inspect', video_row)
    specs = []
    for i in range(2):
        path = tmp_path / f'cam{i}' / 'a.mp4'
        path.parent.mkdir()
        path.write_bytes(f'camera{i}'.encode())
        specs.append(dict(kind='video', path=str(path.parent), camera=f'视角0{i+1}'))
    options = dict(category='calving', farm=str(tmp_path / 'farm'), cache=tmp_path / 'cache')
    plan = v.plan_import(tmp_path / 'farm', specs, **options)
    plan['streaming'] = True
    job = tmp_path / 'job'
    job.mkdir()
    (job / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    result = v.organize(tmp_path / 'farm', specs[:1], job=job, **options)
    assert result['total_files'] == 1 and result['archived_files'] == 1
    assert not Path(next(r['target'] for r in plan['rows'] if r['owner'] == '视角02')).exists()


def test_nested_unchecked_view_is_excluded_from_parent_selection(organizer, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtCore import Qt

    from cowmata_tailring.workspace import video_intake as v
    root = tmp_path / '乐橙'
    for file in (root / 'one.mp4', root / '右1/two.mp4'):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(file.name.encode())
    organizer.set_video_root(root)
    wait_for_views(organizer)
    for i in range(organizer.sources.rowCount()):
        path = Path(organizer.sources.item(i, 1).data(Qt.ItemDataRole.UserRole))
        if path == root:
            organizer.sources.item(i, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(v, 'inspect', video_row)
    plan = v.plan_import(tmp_path / 'farm', organizer.source_specs(), category='calving',
                         farm=str(tmp_path / 'farm'), cache=tmp_path / 'cache')
    assert {Path(r['source']) for r in plan['rows']} == {root / 'one.mp4'}


def test_old_pending_task_applies_new_deletion_policy(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v
    monkeypatch.setattr(v, 'inspect', video_row)
    path = tmp_path / 'camera/blank.mp4'
    path.parent.mkdir()
    path.write_bytes(bytes(1024))
    specs = [dict(kind='video', path=str(path.parent), camera='视角01')]
    options = dict(category='calving', farm=str(tmp_path / 'farm'), cache=tmp_path / 'cache')
    old = v.plan_import(tmp_path / 'farm', specs, **options)
    old['streaming'] = True
    job = tmp_path / 'job'
    job.mkdir()
    (job / 'plan.json').write_text(json.dumps(old), encoding='utf-8')
    result = v.organize(tmp_path / 'farm', specs, job=job, delete_unusable=True, **options)
    assert not path.exists(), 'An old ready row bypassed the new health policy'
    assert result['counts']['deleted'] == 1
    assert result['archived_files'] == 0
