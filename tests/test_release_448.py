import json
import shutil
import zipfile
from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.collaboration_packages import ASSIGNMENT, MANIFEST, dispatch, inventory, open_raw_package, plan_dispatch, receive_return
from cowmata_tailring.workspace.storage import atomic_json
from test_collaboration_packages_396 import farm, prepared  # noqa: F401
from test_collaboration_445 import annotate, copy_by_hand, zip_folder


def test_legacy_zip_accepts_directory_entries_and_wrapper(farm, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊'), count=1))[0]
    archive = tmp_path / 'wrapped.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(package.name + '/', b'')
        for path in sorted(package.rglob('*')):
            rel = path.relative_to(package).as_posix()
            if path.is_dir():
                z.writestr(package.name + '/' + rel + '/', b'')
            else:
                z.write(path, package.name + '/' + rel)
    opened = open_raw_package(archive, tmp_path / 'opened')
    assert opened.name == farm.name and (opened / ASSIGNMENT).is_file()


def test_receive_ignores_manifest_extra_duplicate(farm, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊'), count=1))[0]
    farm_dir = copy_by_hand(package, tmp_path / 'worker') / farm.name
    label = annotate(farm_dir)
    from cowmata_tailring.workspace.collaboration_packages import make_return
    result = make_return(farm_dir)
    duplicate = result / label.relative_to(farm_dir).with_name(label.stem + ' (1)' + label.suffix)
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result / label.relative_to(farm_dir), duplicate)
    report = receive_return(farm, result)
    assert report['imported'] == 1
    assert report['ignored_extra'] and report['ignored_extra'][0]['identical'] is True


def test_dispatch_includes_previous_day_late_recording(farm):
    video = farm / '录像' / '2026-09-16' / '视角01' / '2026-09-16_23-40-00.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'prev')
    unit = next(u for u in inventory(farm, '产犊') if u['day'] == '2026-09-17')
    plan = plan_dispatch(farm, [unit], count=1)[0]
    paths = {e['path'] for e in plan['entries']}
    assert '录像/2026-09-16/视角01/2026-09-16_23-40-00.mp4' in paths
    assert any('已补入相邻日期' in w for w in plan['readiness']['warnings'])
    assert any('视角01 录像' in w for w in plan['readiness']['warnings'])


def _suite(home, version='behavior-test'):
    root = home / 'versions' / version
    root.mkdir(parents=True)
    (root / 'report.json').write_text('{}', encoding='utf-8')
    (root / 'suite.json').write_text(json.dumps({
        'schema': 'cowmata-event-suite-1', 'complete': True, 'feature_version': 'ppg-second-features-1',
        'version': version, 'report': 'report.json', 'modality': 'motion',
        'models': [{'code': 'TAIL_RAISED', 'file': 'model.json', 'threshold': 0.5, 'sha256': '0'*64}],
    }), encoding='utf-8')
    (home / 'active.json').write_text(json.dumps({'version': version}), encoding='utf-8')


def test_bundled_behavior_fallback(monkeypatch, tmp_path):
    import cowmata_tailring.algorithms.paths as paths
    from cowmata_tailring.algorithms.adapter import available_pack
    bundled = tmp_path / 'app' / 'models' / '行为识别'
    _suite(bundled)
    monkeypatch.setattr(paths, 'APP_ROOT', tmp_path / 'app')
    monkeypatch.setattr('cowmata_tailring.algorithms.registry.APP_ROOT', tmp_path / 'app')
    monkeypatch.setenv('COWMATA_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('COWMATA_SITE_TREE', '0')
    monkeypatch.setenv('COWMATA_MODEL_DISCOVERY', '0')
    monkeypatch.delenv('COWMATA_ALGORITHM_HOME', raising=False)
    paths._DISCOVERY.clear()
    pack = available_pack()
    assert pack and pack['version'] == 'behavior-test'


def test_forward_window_missing_packages_and_command(qt_application):
    from cowmata_tailring.edge_download.forward_decision import ForwardDecisionWindow, build_command
    app = QApplication.instance() or QApplication([])
    win = ForwardDecisionWindow(None)
    win.refresh_state()
    assert '未找到' in win.status.text() or '正向决策器' in win.status.text()
    cmd = build_command(__import__('pathlib').Path('C:/pkg'), 'decide', '金姆脉诊')
    assert cmd[-3:] == ['decide', '--ppg', '金姆脉诊']
