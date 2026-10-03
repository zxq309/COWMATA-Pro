"""4.4.5: collaboration packages are folders written straight into the folder the operator picks.

Dispatch, annotation returns and receiving never compress; receiving moves the return folder to the
chosen place afterwards. ≤4.4.4 ZIPs are still opened and received.
"""
import errno
import json
import os
import zipfile
from pathlib import Path

import pytest

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace import collaboration_packages as packages
from cowmata_tailring.workspace.catalog import digest_file
from cowmata_tailring.workspace.collaboration_packages import (
    ASSIGNMENT,
    MANIFEST,
    destination_candidates,
    dispatch,
    inventory,
    make_return,
    move_package,
    open_raw_package,
    plan_dispatch,
    read_package,
    receive_return,
)
from cowmata_tailring.workspace.farm_layout import collaboration_home
from cowmata_tailring.workspace.label_file import build_label_file
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork

from test_collaboration_packages_396 import farm  # noqa: F401  (shared fixture)


def files_of(folder):
    return sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob('*') if p.is_file())


def annotate(farm_dir, t0=100):
    raw = next((farm_dir / '产犊/Motion').rglob('*.json'))
    work = SessionWork(digest_file(raw))
    work.project.cow_id = raw.parent.name.split('-')[1]
    work.set_category('calving')
    work.project.add_event(0, t0, 300)
    label = (farm_dir / '产犊/标注工程' / raw.relative_to(farm_dir / '产犊')).with_suffix('.标注.json')
    atomic_json(label, build_label_file(work, load_motion_json(raw), farm_dir, [], {}, include_record=False))
    return label


def zip_folder(folder, target):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in folder.rglob('*') if p.is_file()):
            archive.write(path, path.relative_to(folder).as_posix())
    return target


@pytest.fixture
def exchange(tmp_path):
    """A folder like F:\\4090转移派包 on the exchange drive."""
    folder = tmp_path / 'X'  # pytest's own temp path is already long; keep below the 260-character limit
    folder.mkdir(parents=True)
    return folder


def test_dispatch_writes_folders_into_the_chosen_target(farm, exchange):
    plans = plan_dispatch(farm, inventory(farm, '产犊'), count=2)
    report = {}
    outputs = dispatch(farm, plans, destination=exchange, report=report)
    assert [p.parent for p in outputs] == [exchange, exchange] and all(p.is_dir() for p in outputs)
    assert [p.name for p in outputs] == [plan['base_name'] + '_原始' for plan in plans]
    assert report['destination'] == str(exchange) and not list(exchange.parent.rglob('*.zip'))
    assert not any(p.name.endswith('.partial') for p in exchange.iterdir())
    manifest = read_package(outputs[0], strict=False)
    listed = {m['path']: m for m in manifest['members']}
    assert set(files_of(outputs[0])) == set(listed) | {MANIFEST}
    for relative, member in listed.items():
        local = outputs[0] / relative
        assert local.stat().st_size == member['size'] and digest_file(local) == member['sha256']
        original = farm / relative.split('/', 1)[1]
        if original.is_file() and not relative.endswith('.cowmata-farm.json'):
            assert local.read_bytes() == original.read_bytes()
    record = json.loads((collaboration_home(farm) / '派发记录' / (plans[0]['package_id'] + '.json')).read_text(encoding='utf-8'))
    assert record['output'] == str(outputs[0]) and record['format'] == 'folder'


def test_target_inside_farm_data_is_refused_but_collaboration_folder_is_allowed(farm):
    plans = plan_dispatch(farm, inventory(farm, '产犊'))
    with pytest.raises(ValueError, match='牧场数据目录'):
        dispatch(farm, plans, destination=farm / '产犊' / '派包')
    with pytest.raises(ValueError, match='完整的目标位置'):
        dispatch(farm, plans, destination='派包')
    assert dispatch(farm, plans, destination=collaboration_home(farm) / '另一个目录')[0].is_dir()


def test_archive_move_that_would_be_too_deep_leaves_the_return_in_place(farm, exchange, tmp_path, monkeypatch):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    annotate(task)
    returned = make_return(task, destination=tmp_path / 'U')
    monkeypatch.setattr(packages, '_MAX_PATH', len(str(tmp_path / 'A')) + len(returned.name) + 20)
    report = receive_return(farm, returned, archive_to=tmp_path / 'A')
    assert report['imported'] == 1 and '路径太深' in report['not_moved'] and returned.is_dir()


def test_too_deep_target_is_refused_before_anything_is_written(farm, exchange, monkeypatch):
    monkeypatch.setattr(packages, '_MAX_PATH', len(str(exchange)) + 120)
    plans = plan_dispatch(farm, inventory(farm, '产犊'))
    with pytest.raises(ValueError, match='路径太深'):
        dispatch(farm, plans, destination=exchange)
    assert list(exchange.iterdir()) == []


def test_open_in_place_or_moved_to_another_folder(farm, exchange, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    assert task == package / farm.name and (task / ASSIGNMENT).is_file()
    assert json.loads((task / ASSIGNMENT).read_text(encoding='utf-8'))['package_id'] == read_package(package, strict=False)['package_id']
    moved = open_raw_package(package, tmp_path / '本机标注')
    assert moved == tmp_path / '本机标注' / package.name / farm.name and not package.exists()
    assert open_raw_package(moved.parent, tmp_path / '本机标注') == moved  # already there: nothing moves


def test_move_between_disks_copies_then_removes_the_source(farm, exchange, tmp_path, monkeypatch):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    before = {p: (package / p).read_bytes() for p in files_of(package)}
    real = os.rename

    def rename(source, target):
        if Path(source) == package:
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        return real(source, target)
    monkeypatch.setattr(os, 'rename', rename)
    moved = move_package(package, tmp_path / 'D盘')
    assert moved == tmp_path / 'D盘' / package.name and not package.exists()
    assert {p: (moved / p).read_bytes() for p in files_of(moved)} == before
    assert not [p for p in (tmp_path / 'D盘').iterdir() if p.name.endswith('.partial')]


def test_cancelled_move_between_disks_keeps_the_source(farm, exchange, tmp_path, monkeypatch):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    real = os.rename

    def rename(source, target):
        if Path(source) == package:
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        return real(source, target)
    monkeypatch.setattr(os, 'rename', rename)
    with pytest.raises(InterruptedError):
        move_package(package, tmp_path / 'D盘', cancelled=lambda: True)
    assert package.is_dir() and read_package(package, strict=False)
    assert not (tmp_path / 'D盘' / package.name).exists()
    assert not [p for p in (tmp_path / 'D盘').iterdir() if p.name.endswith('.partial')]


def test_return_folder_goes_to_the_chosen_target_and_is_received_then_archived(farm, exchange, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    label = annotate(task)
    returned = make_return(task, destination=tmp_path / 'U盘')
    assert returned.parent == tmp_path / 'U盘' and returned.name.startswith(package.name[:-len('_原始')] + '_标注_')
    assert files_of(returned) == sorted([MANIFEST, label.relative_to(task).as_posix()])
    archive = tmp_path / 'A'  # pytest's temp path is long; the default 已接收 folder sits deeper
    report = receive_return(farm, returned, archive_to=archive)
    assert report['imported'] == 1 and report['moved_to'] == str(archive / returned.name) and not returned.exists()
    assert (farm / label.relative_to(task)).is_file()
    again = receive_return(farm, archive / returned.name, archive_to=archive)  # received twice: nothing new, stays put
    assert again['unchanged'] == 1 and again['moved_to'] == str(archive / returned.name)


def test_default_return_target_is_next_to_the_raw_package(farm, exchange):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    annotate(task)
    assert make_return(task).parent == exchange


@pytest.mark.parametrize('tamper', ['extra_file', 'changed_label', 'missing_label'])
def test_tampered_return_folder_is_refused_and_not_moved(farm, exchange, tmp_path, tamper):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    label = annotate(task)
    returned = make_return(task, destination=tmp_path / 'U盘')
    inside = returned / label.relative_to(task)
    if tamper == 'extra_file':
        (returned / 'readme.txt').write_text('x', encoding='utf-8')
        report = receive_return(farm, returned, archive_to=collaboration_home(farm) / '已接收')
        assert report['imported'] == 1 and report['ignored_extra'][0]['path'] == 'readme.txt'
        assert not returned.exists() and (farm / label.relative_to(task)).exists()
        return
    elif tamper == 'changed_label':
        inside.write_bytes(inside.read_bytes()[:-1] + b' ')  # same size, different bytes
    else:
        inside.unlink()
    with pytest.raises(ValueError, match='清单'):
        receive_return(farm, returned, archive_to=collaboration_home(farm) / '已接收')
    assert returned.is_dir() and not (farm / label.relative_to(task)).exists()


def test_conflicting_return_is_moved_beside_its_report(farm, exchange, tmp_path, monkeypatch):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    label = annotate(task)
    returned = make_return(task, destination=tmp_path / 'U盘')
    local = farm / label.relative_to(task)
    doc = json.loads(label.read_text(encoding='utf-8'))
    doc['work']['project']['events'][0]['t0'] = 150
    atomic_json(local, doc)
    before = local.read_bytes()
    monkeypatch.setattr(packages, '_MAX_PATH', 400)  # 冲突待核对 inside pytest's long temp path; only folders are checked below
    report = receive_return(farm, returned, archive_to=collaboration_home(farm) / '已接收')
    assert report['conflicts'] and report['imported'] == 0 and local.read_bytes() == before
    held = Path(report['archive'])
    assert held.parent.name == '冲突待核对' and report['moved_to'] == str(held / returned.name)
    assert (held / returned.name).is_dir() and (held / '核验报告.json').is_file() and not returned.exists()


def test_return_of_another_farm_stays_where_it_is(farm, exchange, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    annotate(task)
    returned = make_return(task, destination=tmp_path / 'U盘')
    other = tmp_path / '别的牧场'
    from cowmata_tailring.workspace.farm_layout import initialize_farm
    initialize_farm(other)
    with pytest.raises(ValueError, match='不属于此牧场'):
        receive_return(other, returned, archive_to=collaboration_home(other) / '已接收')
    assert returned.is_dir()


def test_legacy_zip_packages_are_still_opened_and_received(farm, exchange, tmp_path):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    raw_zip = zip_folder(package, tmp_path / (package.name + '.zip'))
    with pytest.raises(ValueError, match='解包位置'):
        open_raw_package(raw_zip)
    task = open_raw_package(raw_zip, tmp_path / '解包')
    assert task.parent.parent == tmp_path / '解包' and (task / ASSIGNMENT).is_file()
    label = annotate(task)
    returned_zip = zip_folder(make_return(task, destination=tmp_path / '回传'), tmp_path / 'old_标注.zip')
    archive = collaboration_home(farm) / '已接收'
    report = receive_return(farm, returned_zip, archive_to=archive)
    assert report['imported'] == 1 and (archive / returned_zip.name).is_file() and not returned_zip.exists()
    assert (farm / label.relative_to(task)).is_file()


def test_redispatch_removes_untouched_old_folders_and_keeps_opened_ones(farm, exchange):
    rows = inventory(farm, '产犊')
    first = dispatch(farm, plan_dispatch(farm, rows, count=2), destination=exchange)
    open_raw_package(first[1])  # someone started on the second package
    again = plan_dispatch(farm, inventory(farm, '产犊'), count=1)
    for plan in again:
        plan['replace_previous'] = True
    report = {}
    dispatch(farm, again, destination=exchange, report=report)
    replaced = report['packages'][0]['replaced']
    assert replaced['removed'] == [str(first[0])] and replaced['kept'] == [str(first[1])]
    assert not first[0].exists() and first[1].is_dir()
    records = {json.loads(p.read_text(encoding='utf-8'))['output'] for p in (collaboration_home(farm) / '派发记录').glob('*.json')}
    assert str(first[1]) in records and str(first[0]) not in records


def test_finished_package_leaves_no_lock_and_probing_never_creates_one(farm, exchange):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    assert sorted(p.name for p in exchange.iterdir()) == [package.name]
    probe = exchange / ('.' + 'e' * 32 + '.partial.lock')
    assert packages._lock_free(probe) is None and not probe.exists()


def test_a_folder_being_finished_is_never_taken_for_abandoned(farm, exchange, monkeypatch):
    """exFAT never updates a folder's modified time: liveness must come from the writer lock alone."""
    seen = []
    real = packages._rename_into_place

    def finishing(source, target):
        # Another dispatch cleans the same exchange folder while this one renames its finished folder.
        seen.append(packages._abandoned(Path(source)))
        packages._remove_stale_partials(Path(source).parent)
        assert Path(source).is_dir()
        return real(source, target)
    monkeypatch.setattr(packages, '_rename_into_place', finishing)
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    assert seen == [False] and read_package(package, strict=False)


def test_receiving_a_raw_package_by_mistake_is_refused_without_reading_it(farm, exchange, monkeypatch):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    monkeypatch.setattr(packages, 'digest_file', lambda *_: pytest.fail('a raw package must not be hashed'))
    with pytest.raises(ValueError, match='原始数据包'):
        receive_return(farm, package, archive_to=collaboration_home(farm) / '已接收')
    assert package.is_dir()


def test_picking_the_farm_folder_inside_the_package_opens_it_in_place(farm, exchange):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    picked = package / farm.name
    default = destination_candidates('open', package=str(picked), drives=[])[0][0]
    assert default == str(exchange)
    assert open_raw_package(picked, default) == picked and package.is_dir()
    assert open_raw_package(picked, package) == picked  # its own folder as target also means "in place"


def test_package_kept_inside_the_dispatcher_farm_is_still_its_own_task(farm):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')))[0]  # default: inside 科牧特_协作标注
    assert package.is_relative_to(farm)
    root, assignment = packages.task_assignment(package)  # 4.4.5 review: was resolved to the dispatcher's farm
    assert root == package / farm.name
    assert assignment['package_id'] == read_package(package, strict=False)['package_id']
    assert destination_candidates('returns', package, drives=[])[0][0] == str(package.parent)


def test_candidates_offer_default_recent_and_exchange_folders(farm, tmp_path):
    drive = tmp_path / 'F盘'
    for name in ('4090转移派包', '照片', '1_下载器'):
        (drive / name).mkdir(parents=True)
    recent = tmp_path / '上次用过'
    recent.mkdir()
    found = destination_candidates('dispatch', farm, recent=[str(recent), str(tmp_path / '已删除')],
                                   drives=[(str(drive), '移动磁盘')])
    paths = [p for p, _ in found]
    assert paths[0] == str(collaboration_home(farm) / '原始数据包') and '默认' in found[0][1]
    assert str(recent) in paths and str(tmp_path / '已删除') not in paths
    assert str(drive) in paths and str(drive / '4090转移派包') in paths
    assert str(drive / '照片') not in paths and str(drive / '1_下载器') not in paths
    assert destination_candidates('receive', farm, drives=[(str(drive), '移动磁盘')])[0][0] == str(collaboration_home(farm) / '已接收')
    package = tmp_path / 'x_原始'
    package.mkdir()
    opened = destination_candidates('open', package=str(package), drives=[])
    assert opened[0] == (str(tmp_path), '默认 · 原位置打开（不移动）')


def test_return_candidates_start_next_to_the_raw_package(farm, exchange):
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    for picked in (package, package / farm.name, package / farm.name / '产犊'):
        assert destination_candidates('returns', picked, drives=[])[0][0] == str(exchange)


def _dispose(widget):
    """Free a test's top-level widget now instead of leaving it to later module teardowns."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    if isValid(widget):
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_dialog_offers_targets_and_dispatches_into_the_chosen_one(farm, exchange, qt_application):
    import time

    from PySide6.QtWidgets import QWidget

    from cowmata_tailring.workspace.collaboration_ui import open_dispatch
    window = QWidget()
    dialog = open_dispatch(window, str(farm))
    try:
        deadline = time.monotonic() + 60
        while (dialog.future is not None or not dialog.planner.inventory_by_category) and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        assert dialog.target.itemText(0) == str(collaboration_home(farm) / '原始数据包')
        dialog.target.setEditText(str(exchange))
        dialog.count.setValue(1)
        dialog.planner.panes[0].selected_days.update({'2026-09-17', '2026-09-18'})
        dialog.planner.refresh()
        dialog.execute()  # previews, then dispatches
        deadline = time.monotonic() + 60
        while (dialog.future is not None or not list(exchange.iterdir())) and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        deadline = time.monotonic() + 60
        while dialog.future is not None and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        made = [p for p in exchange.iterdir() if p.name.endswith('_原始')]
        assert len(made) == 1 and made[0].is_dir()
        assert '派发到：' + str(exchange) in dialog.log.toPlainText() or '已生成' in dialog.log.toPlainText()
    finally:
        dialog.timer.stop()
        dialog.pool.shutdown(wait=True)
        dialog.close()
        window.close()
        _dispose(window)


def test_modal_dialogs_are_freed_after_use(qt_application, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QDialog, QWidget

    from cowmata_tailring.workspace.collaboration_ui import CollaborationDialog, open_dialog
    monkeypatch.setattr(CollaborationDialog, 'exec', lambda self: QDialog.DialogCode.Rejected)
    window = QWidget()
    try:
        for mode in ('returns', 'receive', 'open'):
            open_dialog(window, mode)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert window.findChildren(CollaborationDialog) == []
    finally:
        _dispose(window)


def test_receive_dialog_takes_folders_and_moves_them(farm, exchange, tmp_path, qt_application, monkeypatch):
    import time

    from cowmata_tailring.workspace.collaboration_ui import KEEP_IN_PLACE, CollaborationDialog
    package = dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊')), destination=exchange)[0]
    task = open_raw_package(package)
    annotate(task)
    returned = make_return(task, destination=tmp_path / 'U盘')
    dialog = CollaborationDialog('receive', None, str(farm))
    try:
        assert dialog.target.itemText(0) == str(collaboration_home(farm) / '已接收')
        assert dialog.target.findText(KEEP_IN_PLACE) >= 0
        dialog.set_packages([str(returned)])
        dialog.target.setEditText(str(tmp_path / 'A'))  # pytest's temp path is long; the default sits deeper
        dialog.execute()
        deadline = time.monotonic() + 60
        while dialog.future is not None and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        qt_application.processEvents()
        log = dialog.log.toPlainText()
        assert '已接收 1' in log and '数据包已移到：' in log and not returned.exists()
        assert (tmp_path / 'A' / returned.name).is_dir() and not dialog.selected_packages
    finally:
        dialog.timer.stop()
        dialog.pool.shutdown(wait=True)
        dialog.close()
        _dispose(dialog)
