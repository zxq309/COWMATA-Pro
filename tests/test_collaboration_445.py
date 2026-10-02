"""4.4.5: annotation returns from raw package folders copied by hand (or ≤4.4.4 ZIPs unzipped by hand),
and receiving after 派发记录 was lost."""
import json
import shutil
import zipfile

import pytest

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import digest_file
from cowmata_tailring.workspace.collaboration_packages import (
    ASSIGNMENT,
    MANIFEST,
    canonical,
    dispatch,
    inventory,
    make_return,
    plan_dispatch,
    read_package,
    receive_return,
)
from cowmata_tailring.workspace.farm_layout import collaboration_home
from cowmata_tailring.workspace.label_file import build_label_file
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork

from test_collaboration_packages_396 import farm  # noqa: F401  (shared fixture)


def copy_by_hand(package, folder):
    """What an annotator gets: the dispatched folder copied with Explorer (USB stick, shared drive …)."""
    folder.mkdir(parents=True, exist_ok=True)
    return shutil.copytree(package, folder / package.name)


def zip_folder(package, target):
    """A ≤4.4.4-style package: the same tree stored in one ZIP."""
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in package.rglob('*') if p.is_file()):
            archive.write(path, path.relative_to(package).as_posix())
    return target


def unzip_by_hand(archive, folder):
    """What Windows “全部解压缩” does: <folder>\\<zip name>\\{协作清单.json, <牧场>\\…}."""
    target = folder / archive.stem
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    return target


def annotate(farm_dir):
    raw = next((farm_dir / '产犊/Motion').rglob('*.json'))
    motion = load_motion_json(raw)
    work = SessionWork(digest_file(raw))
    work.project.cow_id = raw.parent.name.split('-')[1]
    work.set_category('calving')
    work.project.add_event(0, 100, 300)
    label = (farm_dir / '产犊/标注工程' / raw.relative_to(farm_dir / '产犊')).with_suffix('.标注.json')
    atomic_json(label, build_label_file(work, motion, farm_dir, [], {}, include_record=False))
    return label


def first_package(farm):
    return dispatch(farm, plan_dispatch(farm, inventory(farm, '产犊'), count=2))[0]


def forget_dispatch_records(farm):
    for record in (collaboration_home(farm) / '派发记录').glob('*.json'):
        record.unlink()


def rewrite_manifest(result, change, folder):
    """A copy of a return whose 协作清单.json was edited (e.g. what 4.4.4 wrote: no source_manifest)."""
    target = shutil.copytree(result, folder / ('edited-' + result.name))
    manifest = json.loads((target / MANIFEST).read_bytes())
    change(manifest)
    (target / MANIFEST).write_bytes(canonical(manifest))
    return target


@pytest.mark.parametrize('pick', ['farm', 'category', 'package_folder'])
def test_return_from_package_folder_copied_by_hand(farm, tmp_path, pick):
    copied = copy_by_hand(first_package(farm), tmp_path / '标准工程')
    farm_dir = copied / farm.name
    assert (copied / MANIFEST).is_file() and not (farm_dir / ASSIGNMENT).exists()
    label = annotate(farm_dir)
    chosen = {'farm': farm_dir, 'category': farm_dir / '产犊', 'package_folder': copied}[pick]
    result = make_return(chosen)
    assert result.is_dir() and result.parent == tmp_path / '标准工程'
    manifest = read_package(result)
    assert manifest['annotations'] == [label.relative_to(farm_dir).as_posix()]
    assert manifest['source_manifest']['package_id'] == manifest['package_id']
    # Adopted exactly as 打开协作数据包 would have written it.
    assert json.loads((farm_dir / ASSIGNMENT).read_text(encoding='utf-8'))['package_id'] == manifest['package_id']
    assert (farm_dir / '产犊/标注工程/annotation-layout.json').is_file()


def test_legacy_zip_unzipped_by_hand_still_returns(farm, tmp_path):
    archive = zip_folder(first_package(farm), tmp_path / 'old_原始.zip')
    farm_dir = unzip_by_hand(archive, tmp_path / 'w') / farm.name
    label = annotate(farm_dir)
    report = receive_return(farm, make_return(farm_dir))
    assert report['imported'] == 1 and (farm / label.relative_to(farm_dir)).is_file()


def test_copied_return_is_received_by_the_dispatcher(farm, tmp_path):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    label = annotate(farm_dir)
    report = receive_return(farm, make_return(farm_dir))
    assert report['imported'] == 1 and not report['conflicts']
    assert (farm / label.relative_to(farm_dir)).is_file()


def test_copied_package_missing_a_raw_file_is_refused(farm, tmp_path):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    annotate(farm_dir)
    next((farm_dir / '产犊/Temp').rglob('*.json')).unlink()
    with pytest.raises(ValueError, match='重新完整复制'):
        make_return(farm_dir)
    assert not (farm_dir / ASSIGNMENT).exists()


def test_manifest_beside_another_farm_is_refused(farm, tmp_path):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    marker = json.loads((farm_dir / '.cowmata-farm.json').read_text(encoding='utf-8'))
    marker['farm_id'] = '00000000-0000-4000-8000-000000000000'
    (farm_dir / '.cowmata-farm.json').write_text(json.dumps(marker), encoding='utf-8')
    with pytest.raises(ValueError, match='同一牧场'):
        make_return(farm_dir)


def test_a_plain_farm_is_still_not_a_task(farm):
    with pytest.raises(ValueError, match='不是已派发任务'):
        make_return(farm)


def test_lost_dispatch_record_is_recovered_from_the_return(farm, tmp_path):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    annotate(farm_dir)
    result = make_return(farm_dir)
    forget_dispatch_records(farm)
    report = receive_return(farm, result)
    assert report['imported'] == 1 and report['recovered_dispatch_record']
    record = json.loads(next((collaboration_home(farm) / '派发记录').glob('*.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'ready' and record['recovered']
    assert receive_return(farm, result)['unchanged'] == 1


def test_old_return_recovers_record_from_kept_raw_package_folder(farm, tmp_path):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    annotate(farm_dir)
    result = rewrite_manifest(make_return(farm_dir), lambda m: m.pop('source_manifest'), tmp_path)
    forget_dispatch_records(farm)
    assert receive_return(farm, result)['imported'] == 1


def test_old_return_recovers_record_from_kept_raw_zip(farm, tmp_path):
    package = first_package(farm)
    farm_dir = copy_by_hand(package, tmp_path / 'w') / farm.name
    annotate(farm_dir)
    result = rewrite_manifest(make_return(farm_dir), lambda m: m.pop('source_manifest'), tmp_path)
    zip_folder(package, package.with_name(package.name + '.zip'))
    shutil.rmtree(package)
    forget_dispatch_records(farm)
    assert receive_return(farm, result)['imported'] == 1


@pytest.mark.parametrize('change', [lambda m: m.pop('source_manifest'),
                                    lambda m: m['source_manifest']['readiness']['warnings'].append('forged')])
def test_unverifiable_return_is_refused_when_record_is_lost(farm, tmp_path, change):
    farm_dir = copy_by_hand(first_package(farm), tmp_path / 'w') / farm.name
    label = annotate(farm_dir)
    result = rewrite_manifest(make_return(farm_dir), change, tmp_path)
    forget_dispatch_records(farm)
    for raw in (collaboration_home(farm) / '原始数据包').iterdir():
        shutil.rmtree(raw)
    with pytest.raises(ValueError, match='派发记录'):
        receive_return(farm, result)
    assert not (farm / label.relative_to(farm_dir)).exists()
    assert not list((collaboration_home(farm) / '派发记录').glob('*.json'))


def test_release_identifies_as_4_4_5():
    from cowmata_tailring import __build__, __version__

    assert tuple(map(int, __version__.split("."))) >= (4, 4, 5) and __build__.startswith("annotator-")
