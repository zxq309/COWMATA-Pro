import hashlib
import json


def video_metadata(start, duration=20000):
    return dict(
        duration_ms=duration,
        needs_review=False,
        intervals=[
            dict(
                wall_start=start,
                wall_end=start + duration,
                media_start=0,
                media_end=duration,
                verified=True,
            )
        ],
        samples=[],
    )


def test_attach_from_recursive_mixed_tree_respects_farm_category_and_date(tmp_path, monkeypatch):
    from datetime import datetime

    from test_resources_v34 import record

    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import execute, plan_import
    from cowmata_tailring.workspace.resource_layout import TZ

    farm = tmp_path / '自选牧场'
    root = farm / '产犊'
    raw = record(root / 'Motion')
    day = datetime(2026, 8, 4, tzinfo=TZ).timestamp() * 1000
    index = {'dataset_category': 'calving', 'records': [dict(kind='imu', path=raw.relative_to(root).as_posix(),
        sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), size=raw.stat().st_size,
        record_start_ms=day-20000, record_end_ms=day+2*86400000, cow_id='00123')]}
    (root / '资源索引.json').write_text(json.dumps(index), encoding='utf-8')
    labels = root / '标注工程/annotations/keep.json'
    labels.parent.mkdir(parents=True)
    labels.write_text('{"original":true}')
    incoming = tmp_path / '2026.8扬州大学高邮牧场测试'
    videos = incoming / '8月4日/录像/视角01/嵌套'
    videos.mkdir(parents=True)
    movie = videos / 'cross.MP4'
    movie.write_bytes(b'cross midnight video')
    later = videos / 'later.mp4'
    later.write_bytes(b'later video')
    unused = incoming / '未整理九轴/bad.json'
    unused.parent.mkdir()
    unused.write_text('not even JSON')
    (incoming / '标签.csv').write_text('untouched')
    monkeypatch.setattr(SourceInspector, 'video', lambda self, path, sha:
        video_metadata(day+28800000-10000 if path == movie else day+28800000+86400000, 20000))
    before = {p: p.read_bytes() for p in (raw, labels, unused, incoming/'标签.csv')}
    plan = plan_import(farm, [dict(kind='auto', path=str(incoming), camera='auto')],
        '2026-08-04', '2026-08-04', category='calving', farm=str(farm),
        scenario='attach_video', cache=tmp_path/'cache')
    assert plan['target'] == str(root)
    assert len(plan['rows']) == 2 and all(r['kind'] == 'video' for r in plan['rows'])
    ready = [r for r in plan['rows'] if r['status'] == 'ready']
    assert len(ready) == 1 and ready[0]['target'].endswith('2026-08-03_23-59-50.mp4')
    assert execute(plan, tmp_path/'job')['completed']
    assert all(p.read_bytes() == content for p, content in before.items())
    assert movie.exists() and later.exists()


def test_reversed_dates_are_rejected_before_reading_media(tmp_path):
    import pytest

    from cowmata_tailring.workspace.resource_import import plan_import
    with pytest.raises(ValueError, match='日期'):
        plan_import(tmp_path/'out', [dict(kind='video', path=str(tmp_path/'missing'))],
                    '2026-09-05', '2026-09-04', category='healthy')


def test_nested_mixed_root_selects_only_raw_imu_and_video(tmp_path, monkeypatch):
    from test_resources_v34 import record

    from cowmata_tailring.workspace import resource_import
    from cowmata_tailring.workspace.probe import SourceInspector

    root = tmp_path / "mixed"
    raw = record(root / "层一/层二")
    movie = root / "录像/视角03/层三/one.mp4"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"video data")
    (root / "说明.txt").write_text("unrelated")
    (root / "meta.json").write_text("{}")
    (root / "one.标注.json").write_text('{"format":"cowmata-annotation"}')
    monkeypatch.setattr(
        SourceInspector, "video", lambda *args, **kwargs: video_metadata(1788219681000 + 28800000)
    )
    plan = resource_import.plan_import(
        tmp_path / "target",
        [{"kind": "auto", "path": str(root), "camera": "auto"}],
        category="calving",
        cache=tmp_path / "cache",
    )
    assert {r["kind"] for r in plan["rows"]} == {"imu", "video"}
    assert len(plan["rows"]) == 2
    video = next(r for r in plan["rows"] if r["kind"] == "video")
    assert video["owner"] == "视角03"
    assert video["target"].endswith("2026-09-01_07-41-21.mp4")
    assert raw.exists()


def test_same_mixed_folder_can_be_added_for_each_type_without_duplicate_error(
    tmp_path, monkeypatch
):
    from test_resources_v34 import record

    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import plan_import

    root = tmp_path / "mixed"
    record(root)
    (root / "one.mp4").write_bytes(b"video data")
    monkeypatch.setattr(
        SourceInspector, "video", lambda *a, **kw: video_metadata(1788219681000 + 28800000)
    )
    plan = plan_import(
        tmp_path / "out",
        [dict(kind="imu", path=str(root)), dict(kind="video", path=str(root), camera="视角02")],
        category="calving",
        cache=tmp_path / "cache",
    )
    assert len(plan["rows"]) == 2


def test_attach_video_uses_existing_imu_scope_and_previous_day_overlap(tmp_path, monkeypatch):
    from test_resources_v34 import record

    from cowmata_tailring.workspace.clocks import wall_ms
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import plan_import

    root = tmp_path / "扬大_高邮牧场/怀孕/孕晚期"
    raw = record(root / "Motion")
    origin = wall_ms("2026-09-01 00:00:00") - 28800000
    value = json.loads(raw.read_text(encoding="utf-8"))
    value["create_time"] = origin
    raw.write_text(json.dumps(value), encoding="utf-8")
    registry = dict(
        dataset_category="pregnancy_late",
        records=[
            dict(
                kind="imu",
                path=raw.relative_to(root).as_posix(),
                sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                size=raw.stat().st_size,
                record_start_ms=origin,
                record_end_ms=origin + 60000,
                cow_id="21100",
            )
        ],
    )
    (root / "资源索引.json").write_text(json.dumps(registry), encoding="utf-8")
    movie = root / "待整理录像/视角01/deep/one.mp4"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"video")
    before = raw.read_bytes()
    monkeypatch.setattr(
        SourceInspector, "video", lambda *a, **kw: video_metadata(origin + 28800000 - 10000)
    )
    plan = plan_import(
        root,
        [dict(kind="auto", path=str(root / "待整理录像"), camera="auto")],
        scenario="attach_video",
        cache=tmp_path / "cache",
    )
    assert plan["target"] == str(root)
    assert plan["category"] == "pregnancy_late"
    assert len(plan["rows"]) == 1 and plan["rows"][0]["kind"] == "video"
    assert plan["rows"][0]["target"].endswith("2026-08-31_23-59-50.mp4")
    assert plan["rows"][0]["matched_imu_records"] == 1
    assert raw.read_bytes() == before
    from cowmata_tailring.workspace.resource_import import execute

    result = execute(plan, tmp_path / "job")
    assert result["completed"]
    assert raw.read_bytes() == before
    assert movie.exists()


def test_ocr_only_opening_can_name_video_without_fake_native_clock(tmp_path):
    from cowmata_tailring.workspace.resource_import import archive_bounds

    path = tmp_path / "ordinary.mp4"
    path.write_bytes(b"ordinary mp4 fixture")
    metadata = dict(
        duration_ms=10000,
        needs_review=True,
        intervals=[],
        samples=[dict(media_ms=1500, wall_ms=101500), dict(media_ms=3500, wall_ms=103500)],
    )
    lo, hi, verified = archive_bounds(path, metadata)
    assert (lo, hi) == (100000, 110000)
    assert not verified
    assert metadata["archive_time"]["basis"] == "opening_image_ocr"


def test_import_reuses_saved_manual_video_readings(tmp_path,monkeypatch):
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import plan_import
    root=tmp_path/'source'
    root.mkdir()
    movie=root/'one.mp4'
    movie.write_bytes(b'video')
    cat=Catalog(root,stability_seconds=0)
    cat.scan(fast=True)
    origin=1788219681000+28800000
    metadata=dict(duration_ms=10000,needs_review=True,intervals=[],samples=[],
        manual_readings=[dict(media_ms=0,wall_ms=origin)],time_engine='native-ps-hk-shenmo-v1')
    cat.index_one('one.mp4',lambda *_:metadata,eager=True)
    cat.close()
    monkeypatch.setattr(SourceInspector,'video',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must retain manual clock')))
    plan=plan_import(tmp_path/'output',[dict(kind='video',path=str(root),camera='视角01')],
        category='calving',cache=tmp_path/'cache')
    row=next(r for r in plan['rows'] if r['kind']=='video')
    assert row['status']=='ready'
    assert row['target'].endswith('2026-09-01_07-41-21.mp4')
    assert row['metadata']['archive_time']['basis']=='manual_opening_reading'


def test_unknown_camera_blocks_before_probe_and_explicit_child_overrides_auto(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import material_source_groups, plan_import
    root=tmp_path/'mixed'
    child=root/'视角03/deep'
    child.mkdir(parents=True)
    (child/'one.mp4').write_bytes(b'video')
    groups=material_source_groups([
        dict(kind='auto',path=str(root),camera='auto'),
        dict(kind='video',path=str(child),camera='视角08')],tmp_path/'out','mixed',lambda:False)
    assert len(groups)==1 and groups[0]['camera']=='视角08'
    unknown=root/'unknown'
    unknown.mkdir()
    (unknown/'two.mp4').write_bytes(b'video')
    monkeypatch.setattr(SourceInspector,'video',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no probe for unknown view')))
    plan=plan_import(tmp_path/'out',[dict(kind='auto',path=str(unknown),camera='auto')],category='calving',cache=tmp_path/'cache')
    assert len(plan['rows'])==1 and plan['rows'][0]['status']=='blocked'


def test_video_only_move_does_not_redirect_or_replace_imu_project(tmp_path, monkeypatch):
    from test_resources_v34 import record

    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.resource_import import execute, plan_import
    source=tmp_path/'old'
    raw=record(source)
    before=raw.read_bytes()
    (source/'标注工程').mkdir()
    (source/'标注工程/project.json').write_text('{"human_setting":"keep_old"}')
    movie=source/'one.mp4'
    movie.write_bytes(b'video')
    target=tmp_path/'out/扬大_高邮牧场/产犊'
    (target/'标注工程').mkdir(parents=True)
    (target/'标注工程/project.json').write_text('{"human_setting":"keep_target"}')
    monkeypatch.setattr(SourceInspector,'video',lambda *a,**kw:video_metadata(1788219681000+28800000))
    plan=plan_import(tmp_path/'out',[dict(kind='video',path=str(source),camera='视角01')],
        category='calving',transfer='move',cache=tmp_path/'cache')
    result=execute(plan,tmp_path/'job')
    assert result['completed'] and not movie.exists()
    assert raw.read_bytes()==before and not (source/'资源迁移.json').exists()
    assert json.loads((target/'标注工程/project.json').read_text())['human_setting']=='keep_target'
