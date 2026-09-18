
import pytest

from cowmata_tailring.workspace.annotation_store import dated_path
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.farm_layout import initialize_farm, shared_farm, video_root
from cowmata_tailring.workspace.resource_layout import resource_context


def test_shared_farm_keeps_category_sensor_scope_and_day(tmp_path):
    farm = tmp_path / "牧场"
    scope = farm / "产犊"
    for rel in ("产犊/Motion/2026-09-18/设备/a.json",
                "产犊/PPG/2026-09-18/设备/b.json",
                "发情/Motion/2026-09-18/设备/c.json",
                "录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4",
                "录像/2026-09-17/视角01/2026-09-17_23-59-00.mp4"):
        path = farm / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
    initialize_farm(farm)
    assert shared_farm(scope / "Motion") == farm
    assert video_root(scope) == farm / "录像"
    assert resource_context(scope / "Motion" / "2026-09-18") == scope
    catalog = Catalog(scope, day="2026-09-18", stability_seconds=0)
    try:
        catalog.scan(fast=True)
        paths = {row["path"] for row in catalog.rows()}
        assert "产犊/Motion/2026-09-18/设备/a.json" in paths
        assert "录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4" in paths
        assert "录像/2026-09-17/视角01/2026-09-17_23-59-00.mp4" in paths
        assert not any(p.startswith("发情/") for p in paths)
        assert catalog.root == farm
        assert catalog.meta == scope / "标注工程"
    finally:
        catalog.close()


def test_dated_annotations_stay_beside_category_for_farm_relative_source(tmp_path):
    meta = tmp_path / "怀孕" / "孕晚期" / "标注工程"
    assert dated_path(meta, "怀孕/孕晚期/Motion/2026-09-18/cow/a.json") == meta / "Motion/2026-09-18/cow/a.标注.json"
    with pytest.raises(ValueError):
        dated_path(meta, "../Motion/2026-09-18/cow/a.json")


def test_unmarked_legacy_project_remains_compatible(tmp_path):
    for folder in ("Motion", "PPG", "Video"):
        (tmp_path / folder).mkdir()
    assert shared_farm(tmp_path) is None
    assert video_root(tmp_path) == tmp_path / "Video"
    assert resource_context(tmp_path / "Motion") == tmp_path


def test_shared_standalone_keeps_video_and_infers_category(tmp_path):
    from cowmata_tailring.workspace.data_category import read_context
    from cowmata_tailring.workspace.standalone import StandaloneCatalog
    farm = tmp_path / 'farm'
    initialize_farm(farm)
    raw = farm / '怀孕/孕晚期/Motion/2026-09-18/cow/a.json'
    raw.parent.mkdir(parents=True)
    raw.write_text('{}')
    video = farm / '录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'video')
    cat = StandaloneCatalog(raw, day='2026-09-18', meta_path=tmp_path / 'session', stability_seconds=0)
    try:
        cat.scan(fast=True)
        assert len(cat.rows(kind='video')) == 1
        assert cat.source_path(cat.rows(kind='video')[0]['path']) == video
        assert read_context(cat.root, cat.raw_relative)['dataset_category'] == 'pregnancy_late'
    finally:
        cat.close()
