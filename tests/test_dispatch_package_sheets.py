import copy

import pytest
from PySide6.QtCore import Qt
from test_collaboration_packages_396 import farm as base_farm

from cowmata_tailring.workspace import collaboration_packages as packages
from cowmata_tailring.workspace.collaboration_ui import CollaborationDialog


@pytest.fixture
def farm(tmp_path, monkeypatch):
    return base_farm.__wrapped__(tmp_path, monkeypatch)


def unit(day, owner, category='产犊', *, done=False, sent=False, missing=False):
    return dict(key=f'{category}/{day}/{owner}', category=category, day=day, owner=owner,
                paths=[f'{category}/{kind}/{day}/{owner}/a.json' for kind in ('Motion', 'PPG', 'Temp')],
                modalities=['Motion', 'PPG'] if missing else ['Motion', 'PPG', 'Temp'], bytes=300,
                annotation_status='done' if done else 'new', annotated_records=int(done),
                annotation_records=1, completed_records=int(done), dispatches=['old'] if sent else [],
                dispatch_status='assigned' if sent else 'new')


@pytest.fixture
def dialog():
    d = CollaborationDialog('dispatch')
    yield d
    d.timer.stop()
    d.pool.shutdown(wait=True)
    d.close()


def test_selected_day_is_visible_once_and_keeps_all_eligible_devices(dialog):
    rows = [unit('2026-09-14', 'cow1'), unit('2026-09-14', 'cow2'), unit('2026-09-15', 'cow3')]
    assert hasattr(dialog, 'planner'), 'Dispatch needs independent package sheets'
    dialog.planner.load_inventory({'产犊': rows})
    pane = dialog.planner.panes[0]
    assert pane.table.rowCount() == 2
    pane.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert pane.selected_days == {'2026-09-14'}
    assert '2026-09-14' in pane.dates_summary.text()
    assert '已选' in pane.table.item(0, 0).text()
    assert [u['owner'] for u in dialog.planner.groups()[0]] == ['cow1', 'cow2']
    assert not hasattr(dialog, 'views'), 'There must be no camera exclusion control'


def test_packages_choose_categories_and_dates_independently_without_duplicate_units(dialog):
    assert hasattr(dialog, 'planner'), 'Dispatch needs independent package sheets'
    p = dialog.planner
    p.panes[1].category.setCurrentText('发情')
    p.load_inventory({'产犊': [unit('2026-09-14', 'cow1'), unit('2026-09-15', 'cow2')],
                      '发情': [unit('2026-09-14', 'cow3', '发情')]})
    p.panes[0].table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    p.panes[1].table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert not (p.panes[2].table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
    p.panes[2].table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    groups = p.groups()
    assert [[u['owner'] for u in g] for g in groups] == [['cow1'], ['cow3'], ['cow2']]
    p.panes[0].clear_dates()
    assert p.panes[2].table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEnabled
    assert not p.panes[0].selected_days
    assert p.panes[1].selected_days == {'2026-09-14'}


def test_balance_keeps_whole_days_and_skips_sent_completed_or_incomplete_data(dialog):
    assert hasattr(dialog, 'planner'), 'Dispatch needs independent package sheets'
    p = dialog.planner
    rows = [unit(f'2026-09-{day:02}', f'{day}-{n}') for day, count in [(10, 4), (11, 2), (12, 1), (13, 1)] for n in range(count)]
    rows += [unit('2026-09-10', 'done', done=True), unit('2026-09-10', 'sent', sent=True), unit('2026-09-11', 'missing', missing=True)]
    p.load_inventory({'产犊': rows})
    p.balance()
    groups = p.groups()
    assert sorted(len(g) for g in groups) == [2, 2, 4]
    assert len({u['key'] for g in groups for u in g}) == 8
    for day in ['2026-09-10', '2026-09-11', '2026-09-12', '2026-09-13']:
        assert sum(any(u['day'] == day for u in g) for g in groups) == 1
    assert all(u['owner'] not in {'done', 'sent', 'missing'} for g in groups for u in g)


def test_category_or_root_changes_clear_stale_selections_and_preview(dialog):
    assert hasattr(dialog, 'planner'), 'Dispatch needs independent package sheets'
    p = dialog.planner
    p.load_inventory({'产犊': [unit('2026-09-14', 'cow1')]})
    p.panes[0].table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dialog.plans = [{'old': True}]
    p.panes[0].category.setCurrentText('发情')
    assert not dialog.plans and not p.panes[0].selected_days
    assert p.panes[0].table.rowCount() == 0
    dialog.root.setText('new-farm')
    assert not p.inventory_by_category


def test_explicit_groups_preserve_selected_days_and_all_video_views(farm):
    rows = packages.inventory(farm, '产犊')
    for path in list((farm / '录像').rglob('*.mp4')):
        second = path.parent.parent / '视角02' / path.name
        second.parent.mkdir()
        second.write_bytes(path.read_bytes())
    groups = [[u for u in rows if u['day'] == '2026-09-17'], [u for u in rows if u['day'] == '2026-09-18']]
    assert hasattr(packages, 'plan_dispatch_groups'), 'Backend must preserve explicit package groups'
    plans = packages.plan_dispatch_groups(farm, groups)
    assert [p['dates'] for p in plans] == [['2026-09-17'], ['2026-09-18']]
    assert [len(p['units']) for p in plans] == [3, 1]
    assert len({p['task_id'] for p in plans}) == 1
    assert [p['part'] for p in plans] == [1, 2]
    assert all(p['parts'] == 2 and p['selected_views'] is None for p in plans)
    for plan in plans:
        assert {e['path'].split('/')[-2] for e in plan['entries'] if e['path'].endswith('.mp4')} == {'视角01', '视角02'}
    assert len({path for plan in plans for path in plan['sensor_paths']}) == 12


def test_explicit_groups_reject_duplicate_or_empty_packages(farm):
    rows = packages.inventory(farm, '产犊')
    assert hasattr(packages, 'plan_dispatch_groups'), 'Backend must reject invalid manual grouping'
    with pytest.raises(ValueError, match='重复'):
        packages.plan_dispatch_groups(farm, [[rows[0]], [copy.deepcopy(rows[0])]])
    with pytest.raises(ValueError, match='未选择|为空'):
        packages.plan_dispatch_groups(farm, [[rows[0]], []])


def test_new_camera_after_preview_requires_a_fresh_all_views_plan(farm):
    units = [u for u in packages.inventory(farm, '产犊') if u['day'] == '2026-09-17']
    plans = packages.plan_dispatch_groups(farm, [units])
    old = next((farm / '录像' / '2026-09-17').rglob('*.mp4'))
    new = old.parent.parent / '视角02' / old.name
    new.parent.mkdir()
    new.write_bytes(old.read_bytes())
    with pytest.raises(ValueError, match='录像范围已变化'):
        packages.dispatch(farm, plans)
    assert not list((farm / '科牧特_协作标注').rglob('*.zip'))


def test_multi_day_package_stays_together(farm):
    rows = packages.inventory(farm, '产犊')
    plans = packages.plan_dispatch_groups(farm, [rows])
    assert len(plans) == 1
    assert plans[0]['dates'] == ['2026-09-17', '2026-09-18']
    assert len(plans[0]['units']) == 4
