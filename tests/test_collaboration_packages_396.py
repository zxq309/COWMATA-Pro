import base64
import csv
import json
import struct
import threading
import zipfile
from datetime import datetime

import pytest

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import digest_file
from cowmata_tailring.workspace.collaboration_packages import (
    dispatch,
    inventory,
    make_return,
    open_raw_package,
    plan_dispatch,
    receive_return,
    validate_archive,
)
from cowmata_tailring.workspace.farm_layout import COLLABORATION, initialize_farm
from cowmata_tailring.workspace.label_file import build_label_file, load_history
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def farm(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download.core import CHINA, Job
    from cowmata_tailring.edge_download.csv_download import run_csv_job
    from cowmata_tailring.edge_download.csv_targets import FILES
    from cowmata_tailring.workspace import package_readiness
    root = tmp_path / '原牧场'
    initialize_farm(root)
    ledger = tmp_path / 'ledgers'
    ledger.mkdir()
    sample_rows, device_rows, birth_rows = [], [], []
    for i, cow in enumerate([23001, 23002, 23003]):
        device = f'546C50CA{0x7FA + i:04X}'
        sample_rows.append(dict(设备号=device, 牛号=f'{cow}-A', 佩戴开始='2026-09-17 00:00:00',
                                佩戴结束='2026-09-19 00:00:00', 监测目的='产犊监测',
                                产犊开始='2026-09-18 08:00:00', 产犊结束='2026-09-18 09:00:00',
                                九轴='有效', 脉搏='有效', 温度='有效', 已删除='0'))
        device_rows.append(dict(设备编码=device, 新佩戴牛号=f'{cow}-A', 日期='2026-09-17',
                                **{'拆除时间(掉落）': '2026-09-19'}, 记录类型='佩戴'))
        birth_rows.append(dict(牛号=str(cow), 生产日期='2026-09-18', 牛场登记生产时间='09:00'))
    for name, rows in zip(FILES, [sample_rows, device_rows, birth_rows]):
        with (ledger / name).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
    frames = b''.join(struct.pack('<I9h', t, *([1] * 9)) for t in range(0, 1001, 20))
    for day, cows in [('2026-09-17', [23001, 23002, 23003]), ('2026-09-18', [23001])]:
        for cow in cows:
            device = f'546C50CA{0x7FA + cow - 23001:04X}'
            owner = f'{device}-{cow}-A'
            stamp = int(datetime.fromisoformat(day).replace(tzinfo=CHINA).timestamp() * 1000)
            for kind in ('Motion', 'PPG', 'Temp'):
                path = root / '产犊' / kind / day / owner / (day + '_00-00-00.json')
                data = dict(device=device, cow_id=str(cow), create_time=stamp)
                data.update(dict(version=2, imu=base64.b64encode(frames).decode()) if kind == 'Motion' else
                            dict(sample_rate_hz=50, data=base64.b64encode(struct.pack('<51I', *range(51))).decode()) if kind == 'PPG' else
                            dict(data=38.5))
                atomic_json(path, data)
        video = root / '录像' / day / '视角01' / (day + '_00-00-00.mp4')
        video.parent.mkdir(parents=True)
        video.write_bytes(b'fixture-video')
    class EmptyClient:
        def __init__(self, *_):
            pass
        def check(self):
            pass
        def listing(self, *_):
            return []
    run_csv_job(Job('http://example.test', root, '产犊', (), ('motion', 'pulse', 'temp'),
                    datetime(2026, 9, 17, tzinfo=CHINA), datetime(2026, 9, 19, tzinfo=CHINA), ledger),
                threading.Event(), client_factory=EmptyClient)
    def fake_probe(path, cancelled):
        start = datetime.strptime(path.stem, '%Y-%m-%d_%H-%M-%S').replace(tzinfo=CHINA).timestamp() * 1000
        return start, start + 3000
    monkeypatch.setattr(package_readiness, 'video_span', fake_probe)
    return root


def prepared(farm, tmp_path):
    rows = inventory(farm, '产犊')
    plans = plan_dispatch(farm, rows, count=2)
    packages = dispatch(farm, plans)
    destination = open_raw_package(packages[0], tmp_path / 'worker')
    raw = next((destination / '产犊/Motion').rglob('*.json'))
    motion = load_motion_json(raw)
    work = SessionWork(digest_file(raw))
    work.project.cow_id = raw.parent.name.split('-')[1]
    work.set_category('calving')
    work.project.add_event(0, 100, 300)
    label = destination / '产犊/标注工程' / raw.relative_to(destination / '产犊')
    label = label.with_suffix('.标注.json')
    atomic_json(label, build_label_file(work, motion, destination, [], {}, include_record=False))
    result = make_return(destination)
    return packages, destination, raw, label, result


def test_balanced_all_records_no_capacity_limit(farm):
    rows = inventory(farm, '产犊')
    plans = plan_dispatch(farm, rows, count=2)
    assert sorted(len(p['units']) for p in plans) == [2, 2]
    chosen = [p for plan in plans for p in plan['sensor_paths']]
    assert len(chosen) == len(set(chosen)) == 12
    assert all(plan['estimated_bytes'] > 0 for plan in plans)
    assert all('max_bytes' not in plan for plan in plans)


def test_raw_return_roundtrip_is_annotation_only_and_idempotent(farm, tmp_path):
    packages, destination, raw, label, result = prepared(farm, tmp_path)
    assert len(packages) == 2 and packages[0] != packages[1]
    with zipfile.ZipFile(result) as archive:
        names = archive.namelist()
        assert not any(name.endswith('.mp4') for name in names)
        assert all('标注工程/' in name or name == '协作清单.json' for name in names)
    report = receive_return(farm, result)
    assert report['imported'] == 1 and report['conflicts'] == []
    assert receive_return(farm, result)['unchanged'] == 1
    target = farm / label.relative_to(destination)
    history = load_history(target)
    assert history.motion is not None and len(history.work.project.events) == 1
    assert history.motion.source_path == farm / raw.relative_to(destination)
    assert all(row['dispatches'] for row in inventory(farm, '产犊'))


def test_conflicting_return_never_overwrites_human_work(farm, tmp_path):
    _, destination, _, label, result = prepared(farm, tmp_path)
    target = farm / label.relative_to(destination)
    original = json.loads(label.read_text(encoding='utf-8'))
    original['work']['project']['events'][0]['t0'] = 150
    atomic_json(target, original)
    before = target.read_bytes()
    report = receive_return(farm, result)
    assert report['conflicts'] and report['imported'] == 0
    assert target.read_bytes() == before


def test_changed_original_blocks_entire_return(farm, tmp_path):
    _, destination, raw, label, result = prepared(farm, tmp_path)
    (farm / raw.relative_to(destination)).write_bytes(b'changed')
    with pytest.raises(ValueError, match='原始|来源'):
        receive_return(farm, result)
    assert not (farm / label.relative_to(destination)).exists()


@pytest.mark.parametrize('name', ['../outside.json', 'C:/escape.json', 'x/CON', 'x/a.json:stream', 'x/a. ', 'x//b'])
def test_unsafe_zip_member_rejected_before_any_extraction(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, '{}')
    with pytest.raises(ValueError):
        validate_archive(archive)
    assert not (tmp_path / 'outside.json').exists()


def test_cancelled_dispatch_never_marks_unit_sent(farm):
    plans = plan_dispatch(farm, inventory(farm, '产犊'), count=1)
    with pytest.raises(InterruptedError):
        dispatch(farm, plans, cancelled=lambda: True)
    assert not any(row['dispatches'] for row in inventory(farm, '产犊'))
    assert not list((farm / COLLABORATION).rglob('*.zip'))


def test_failed_publication_removes_new_labels_only(farm, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import collaboration_packages as packages
    _, destination, _, label, result = prepared(farm, tmp_path)
    target = farm / label.relative_to(destination)
    real = packages.atomic_json
    def fail(path, value, **kwargs):
        if path.name == '事务.json' and value.get('created'):
            raise OSError('injected publication failure')
        return real(path, value, **kwargs)
    monkeypatch.setattr(packages, 'atomic_json', fail)
    with pytest.raises(OSError, match='injected'):
        receive_return(farm, result)
    assert not target.exists()


def test_interrupted_publication_is_recovered_on_next_receive(farm, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import collaboration_packages as packages
    _, destination, _, label, result = prepared(farm, tmp_path)
    target = farm / label.relative_to(destination)
    real = packages.atomic_json
    def crash(path, value, **kwargs):
        if path.name == '事务.json' and value.get('created'):
            raise KeyboardInterrupt('power loss simulation')
        return real(path, value, **kwargs)
    monkeypatch.setattr(packages, 'atomic_json', crash)
    with pytest.raises(KeyboardInterrupt):
        receive_return(farm, result)
    assert target.exists()
    monkeypatch.setattr(packages, 'atomic_json', real)
    assert receive_return(farm, result)['imported'] == 1


def test_raw_manifest_cannot_authorize_unrelated_json(farm):
    from cowmata_tailring.workspace.collaboration_packages import _raw_relative
    manifest = {'units': [{'category': '产犊', 'day': '2026-09-18', 'owner': 'cow', 'paths': ['config.json']}]}
    assert not _raw_relative('config.json', manifest)


def test_completed_labels_are_review_tasks_with_baseline(farm, tmp_path):
    _, destination, raw, label, result = prepared(farm, tmp_path)
    receive_return(farm, result)
    local = farm / label.relative_to(destination)
    doc = json.loads(local.read_text(encoding='utf-8'))
    doc['work']['progress']['status'] = 'done'
    atomic_json(local, doc)
    unit = next(u for u in inventory(farm, '产犊') if raw.relative_to(destination).as_posix() in u['paths'])
    assert unit['annotation_status'] == 'done'
    with pytest.raises(ValueError, match='复核任务'):
        plan_dispatch(farm, [unit])
    plan = plan_dispatch(farm, [unit], purpose='review')
    package = dispatch(farm, plan)[0]
    reviewed = open_raw_package(package, tmp_path / 'reviewer')
    saved = reviewed / local.relative_to(farm)
    assert saved.exists()
    review_doc = json.loads(saved.read_text(encoding='utf-8'))
    assert review_doc['work']['progress']['status'] == 'done'
    assert review_doc['work']['project']['events'] == doc['work']['project']['events']
    assert validate_archive(make_return(reviewed))['purpose'] == 'review'


@pytest.mark.parametrize('kind', ['Motion', 'PPG', 'Temp'])
def test_missing_modality_cannot_be_dispatched(farm, kind):
    next((farm / '产犊' / kind).rglob('*.json')).unlink()
    with pytest.raises(ValueError, match='缺少'):
        plan_dispatch(farm, inventory(farm, '产犊'))
    assert not list((farm / COLLABORATION).rglob('*.zip'))


def test_missing_or_short_video_never_has_confirmation_bypass(farm, monkeypatch):
    from cowmata_tailring.workspace import package_readiness as readiness
    for path in (farm / '录像').rglob('*.mp4'):
        path.unlink()
    with pytest.raises(ValueError, match='缺少.*录像'):
        plan_dispatch(farm, inventory(farm, '产犊'))
    path = farm / '录像/2026-09-17/视角01/2026-09-17_00-00-00.mp4'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(readiness, 'video_span', lambda *_: (0, 1))
    with pytest.raises(ValueError, match='未覆盖'):
        plan_dispatch(farm, inventory(farm, '产犊'))


@pytest.mark.parametrize('status', ['running', 'failed', 'canceled', 'outdated'])
def test_unfinished_download_cycle_blocks_dispatch(farm, status):
    path = farm / '.edge-download/csv-cycle.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    state['status'] = status
    atomic_json(path, state)
    with pytest.raises(ValueError, match='尚未完整结束'):
        plan_dispatch(farm, inventory(farm, '产犊'))


def test_csv_changes_block_preview_and_publication(farm):
    from cowmata_tailring.edge_download.csv_targets import FILES
    plans = plan_dispatch(farm, inventory(farm, '产犊'))
    cycle = json.loads((farm / '.edge-download/csv-cycle.json').read_text(encoding='utf-8'))
    from pathlib import Path
    ledger = Path(cycle['ledger_directory']) / FILES[0]
    original = ledger.read_bytes()
    ledger.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='CSV 已更新'):
        plan_dispatch(farm, inventory(farm, '产犊'))
    ledger.write_bytes(original)
    changed = False
    def change_during_write(*_):
        nonlocal changed
        if not changed:
            ledger.write_bytes(original + b'\n')
            changed = True
    with pytest.raises(ValueError, match='CSV 已更新'):
        dispatch(farm, plans, progress=change_during_write)
    assert not list((farm / COLLABORATION).rglob('*.zip'))


def test_new_data_is_reported_as_supplement_and_stale_plan_rejected(farm):
    rows = inventory(farm, '产犊')
    plans = plan_dispatch(farm, rows)
    dispatch(farm, plans)
    assert all(u['dispatch_status'] == 'assigned' for u in inventory(farm, '产犊'))
    source = farm / rows[0]['paths'][0]
    data = json.loads(source.read_text(encoding='utf-8'))
    data['create_time'] += 1000
    atomic_json(source.with_name(source.name.replace('00-00-00', '00-00-01')), data)
    latest = inventory(farm, '产犊')
    assert sum(u['new_records'] for u in latest) == 1
    assert any(u['dispatch_status'] == 'supplement' for u in latest)
    with pytest.raises(ValueError, match='新增或删除'):
        dispatch(farm, plans)


def test_downloader_and_dispatch_share_exclusive_lock(farm):
    from cowmata_tailring.edge_download.deduplication import RootSyncLock
    with RootSyncLock(farm, threading.Event()):
        with pytest.raises(ValueError, match='下载正在写入'):
            plan_dispatch(farm, inventory(farm, '产犊'))


@pytest.mark.parametrize('change', ['bad_json', 'wrong_identity', 'wrong_date'])
def test_bad_sensor_data_blocks_dispatch(farm, change):
    raw = next((farm / '产犊/Motion').rglob('*.json'))
    data = json.loads(raw.read_text(encoding='utf-8'))
    if change == 'bad_json':
        raw.write_text('{incomplete', encoding='utf-8')
    else:
        data.update({'cow_id': '99999'} if change == 'wrong_identity' else {'create_time': data['create_time'] + 86400000})
        atomic_json(raw, data)
    with pytest.raises(ValueError):
        plan_dispatch(farm, inventory(farm, '产犊'))


def test_classified_video_filename_uses_beijing_epoch(monkeypatch, tmp_path):
    from cowmata_tailring.edge_download.core import CHINA
    from cowmata_tailring.workspace import video_intake
    from cowmata_tailring.workspace.package_readiness import video_span
    monkeypatch.setattr(video_intake, 'probe', lambda *_: dict(streams=[dict(codec_type='video')], format=dict(duration='3')))
    path = tmp_path / '2026-09-17_00-00-00.mp4'
    start = datetime(2026, 9, 17, tzinfo=CHINA).timestamp() * 1000
    assert video_span(path, lambda: False) == (start, start + 3000)
    with pytest.raises(ValueError, match='时间戳'):
        video_span(tmp_path / 'mb0000.mp4', lambda: False)


def test_reverified_today_does_not_erase_historical_readiness(farm):
    from cowmata_tailring.edge_download.core import CHINA, Job, Result
    from cowmata_tailring.edge_download.csv_targets import CsvPlan
    from cowmata_tailring.edge_download.download_cycle import record_cycle
    state = json.loads((farm / '.edge-download/csv-cycle.json').read_text(encoding='utf-8'))
    job = Job('http://example.test', farm, '产犊', (), (), datetime(2026, 9, 18, tzinfo=CHINA),
              datetime(2026, 9, 19, tzinfo=CHINA), state['ledger_directory'])
    with record_cycle(job, CsvPlan(job.ledger_directory), Result()):
        pass
    assert plan_dispatch(farm, inventory(farm, '产犊'))
