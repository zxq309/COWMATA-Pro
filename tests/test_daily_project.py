import json


def test_picker_reads_one_level_at_a_time(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.project_picker import (
        category_choices,
        date_choices,
        stage_choices,
    )

    farm = tmp_path / "扬大_高邮牧场"
    for folder in (
        "产犊/Motion/2026-08-03/deep",
        "怀孕/孕晚期/Motion/2026-08-04",
        "正常/PPG/2026-08-05",
    ):
        (farm / folder).mkdir(parents=True)
    monkeypatch.setattr(
        type(farm),
        "rglob",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no recursive picker")),
    )
    assert [p.name for p in category_choices(farm)] == ["产犊", "怀孕", "正常"]
    assert [p.name for p in stage_choices(farm / "怀孕")] == ["孕晚期"]
    assert date_choices(farm / "产犊", "Motion") == ["2026-08-03"]


def test_day_scan_does_not_visit_other_dates_or_mark_them_missing(tmp_path, monkeypatch):
    from test_resources_v34 import record

    from cowmata_tailring.workspace.catalog import Catalog

    root = tmp_path / "产犊"
    first = record(root / "Motion/2026-08-03")
    other = record(root / "Motion/2026-08-04")
    video = root / "Video/2026-08-03/视角01/one.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (root / "PPG").mkdir()
    cat = Catalog(root, stability_seconds=0)
    cat.scan(fast=True)
    cat.close()
    import os

    walk = os.walk
    visits = []

    def guarded(path, *args, **kwargs):
        assert str(path) != str(root)
        assert "Motion\\2026-08-04" not in str(path)
        visits.append(str(path))
        return walk(path, *args, **kwargs)

    cat = Catalog(root, stability_seconds=0, day="2026-08-03")
    monkeypatch.setattr(os, "walk", guarded)
    result = cat.scan(fast=True)
    assert result.complete and visits
    assert {r["path"] for r in cat.rows()} == {
        first.relative_to(root).as_posix(),
        video.relative_to(root).as_posix(),
    }
    state = cat.db.execute(
        "SELECT state FROM locations WHERE path=?", (other.relative_to(root).as_posix(),)
    ).fetchone()[0]
    assert state != "missing"
    cat.close()


def test_both_open_buttons_use_picker_and_exports_live_in_dataset_menu(monkeypatch):
    from PySide6.QtWidgets import QAbstractButton, QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr(MainWindow, "choose_project", lambda self: calls.append("picker"))
    window = MainWindow()
    menus = {a.text().split("(")[0]: a.menu() for a in window.menuBar().actions()}
    next(a for a in menus["文件"].actions() if a.text() == "打开工程…").trigger()
    next(b for b in window.findChildren(QAbstractButton) if b.toolTip() == "打开工程").click()
    assert calls == ["picker", "picker"]
    assert not any(a.text() == "导出" for a in menus["文件"].actions())
    assert any(a.text() == "标注分享与片段" for a in menus["数据集构建"].actions())
    window.close()
    app.processEvents()


def test_daily_sessions_do_not_resume_another_days_unfinished_event(tmp_path):
    from cowmata_tailring.workspace.catalog import Catalog

    root = tmp_path / "产犊"
    for kind in ("Motion", "Video", "PPG"):
        for day in ("2026-08-03", "2026-08-04"):
            (root / kind / day).mkdir(parents=True)
    first = Catalog(root, day="2026-08-03")
    first.save_settings(
        dict(
            active_day="2026-08-03",
            active_event={"label": 1},
            current_path="Motion/2026-08-03/one.json",
        )
    )
    first.close()
    second = Catalog(root, day="2026-08-04")
    assert second.settings().get("active_event") is None
    second.close()
    first = Catalog(root, day="2026-08-03")
    assert first.settings()["active_event"] == {"label": 1}
    first.close()


def test_organized_video_reuses_verified_import_clock_without_hash_or_ocr(tmp_path, monkeypatch):
    import hashlib

    from cowmata_tailring.media.native_ps import SIGNATURE
    from cowmata_tailring.workspace import catalog as module

    root = tmp_path / "产犊"
    movie = root / "Video/2026-08-03/视角01/2026-08-03_12-00-00.mp4"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"video")
    for name in ("Motion/2026-08-03", "PPG"):
        (root / name).mkdir(parents=True)
    sha = hashlib.sha256(movie.read_bytes()).hexdigest()
    metadata = dict(
        duration_ms=1000, time_engine=SIGNATURE, camera="视角01", needs_review=True, intervals=[]
    )
    registry = dict(
        records=[
            dict(
                kind="video",
                path=movie.relative_to(root).as_posix(),
                sha256=sha,
                verified_stamp=module.file_stamp(movie),
                size=movie.stat().st_size,
                metadata=metadata,
            )
        ]
    )
    (root / "资源索引.json").write_text(json.dumps(registry), encoding="utf-8")
    cat = module.Catalog(root, day="2026-08-03", stability_seconds=0)
    cat.scan(fast=True)
    monkeypatch.setattr(
        module,
        "digest_file",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("already verified unchanged content")
        ),
    )
    result = cat.index_one(
        movie.relative_to(root).as_posix(),
        lambda *a: (_ for _ in ()).throw(AssertionError("do not re-run OCR")),
        eager=True,
    )
    assert result["asset_id"] == sha and result["state"] == "review"
    cat.close()


def test_canonical_annotation_path_matches_raw_tree_and_opens_history(tmp_path):
    from test_resources_v34 import record

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.workspace.annotation_store import work_document
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.label_file import load_history
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.storage import atomic_json
    from cowmata_tailring.workspace.work import SessionWork

    root = tmp_path / "产犊"
    raw = record(root / "Motion/2026-08-03")
    for name in ("Video", "PPG"):
        (root / name).mkdir()
    cat = Catalog(root, stability_seconds=0, day="2026-08-03")
    cat.scan(fast=True)
    row = cat.index_one(
        raw.relative_to(root).as_posix(), SourceInspector(root, cat.meta), eager=True
    )
    work = SessionWork(row["asset_id"])
    path = cat.work_path(work.asset_id)
    assert path == cat.meta / raw.relative_to(root).with_suffix(".标注.json")
    document = work_document(cat, work, load_motion_json(raw), {})
    assert document["embedded_imu"] is None
    atomic_json(path, document)
    loaded = load_history(path)
    assert loaded.motion and loaded.motion.sample_count == 4
    assert (
        SessionWork.from_dict(json.loads(path.read_text(encoding="utf-8"))).asset_id
        == work.asset_id
    )
    cat.close()
