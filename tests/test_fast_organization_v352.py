# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name
import importlib
import threading
from pathlib import Path

import pytest
from test_fixes_v351 import organizer  # noqa: F401


def api():
    try:
        return importlib.import_module("cowmata_tailring.workspace.video_intake")
    except ModuleNotFoundError:
        pytest.fail("3.5.2 opening-only video intake is missing")


def test_first_frame_owns_midnight_without_subtracting_pts(tmp_path, monkeypatch):
    m = api()
    from PIL import Image

    from cowmata_tailring.workspace.clocks import wall_ms

    movie = tmp_path / "wrong_1999.mp4"
    movie.write_bytes(b"\x00\x00\x01\xba" + b"x" * 200)
    monkeypatch.setattr(
        m,
        "probe",
        lambda *a: {"streams": [{"codec_type": "video"}], "format": {"format_name": "mpeg"}},
    )
    calls = []

    def opening(*args):
        calls.append(args[1])
        return Image.new("RGB", (100, 100)), 40

    monkeypatch.setattr(m, "opening_frame", opening)
    monkeypatch.setattr(
        m, "read_clock", lambda *a: {"success": True, "wall_ms": wall_ms("2026-08-17 00:00:00")}
    )
    result = m.inspect(movie, tmp_path / "cache")
    assert result["record_date"] == "2026-08-17"
    assert result["metadata"]["start_display"] == "2026-08-17_00-00-00"
    assert result["metadata"]["intervals"] == []
    assert result["metadata"]["needs_review"] is True
    assert calls == [0]
    assert Path(result["preview_path"]).is_file()


def test_confirmed_nonvideo_and_uncertain_media_differ(tmp_path, monkeypatch):
    m = api()
    text = tmp_path / "fake.mp4"
    text.write_text("This is a text file")
    assert m.inspect(text, tmp_path / "cache")["kind"] == "nonvideo"
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x01\xba\xff\x00")

    def fail(*a):
        raise ValueError("cannot decode")

    monkeypatch.setattr(m, "probe", fail)
    row = m.inspect(broken, tmp_path / "cache")
    assert row["kind"] == "video"
    assert row["status"] == "blocked"
    assert broken.exists()


def test_completed_item_is_yielded_before_slow_item_finishes():
    m = api()
    release = threading.Event()
    entered = threading.Event()

    def work(value):
        if value == "slow":
            entered.set()
            assert release.wait(5)
        return value

    results = m.parallel_items(["slow", "fast"], work, workers=2)
    try:
        assert next(results) == "fast"
        assert entered.wait(1)
    finally:
        release.set()
    assert list(results) == ["slow"]


def test_video_source_keeps_labels_imu_and_project_files(tmp_path):
    m = api()
    for name in ("raw.json", "标签.csv", "calibration.csv", "one.标注.json", "资源索引.json"):
        assert m.protected_file(tmp_path / name)
    assert m.protected_file(tmp_path / "标注工程" / "notes.txt")
    assert m.protected_file(tmp_path / "Motion" / "original.bin")
    assert m.protected_file(tmp_path / "人工标签.txt")
    assert not m.protected_file(tmp_path / "readme.txt")


def test_streamed_table_updates_existing_row_instead_of_duplicate():
    from cowmata_tailring.workspace.organization_ui import PlanModel

    model = PlanModel()
    update = getattr(model, "update_row", None)
    assert callable(update), "per-file GUI updates are missing"
    update({"source": "a.mp4", "status": "ready"})
    update({"source": "b.mp4", "status": "blocked"})
    update({"source": "a.mp4", "status": "done"})
    assert model.rowCount() == 2
    assert model.rows[0]["status"] == "done"


def test_fast_plan_keeps_all_videos_on_imu_day_and_no_cow_binding(tmp_path, monkeypatch):
    m = api()
    import json
    from datetime import datetime

    from test_resources_v34 import record

    from cowmata_tailring.workspace import resource_import as r
    from cowmata_tailring.workspace.resource_layout import TZ

    root = tmp_path / "farm" / "产犊"
    raw = record(root / "Motion")
    day = datetime(2026, 8, 17, tzinfo=TZ).timestamp() * 1000
    (root / "资源索引.json").write_text(
        json.dumps(
            {
                "dataset_category": "calving",
                "records": [
                    {
                        "kind": "imu",
                        "path": raw.relative_to(root).as_posix(),
                        "size": raw.stat().st_size,
                        "record_start_ms": day + 3600000,
                        "record_end_ms": day + 3601000,
                        "cow_id": "00123",
                    }
                ],
            }
        )
    )
    incoming = tmp_path / "incoming" / "视角02"
    incoming.mkdir(parents=True)
    movie = incoming / "one.mp4"
    movie.write_bytes(b"video")

    def read(path, *a):
        from cowmata_tailring.workspace.organization import identity

        return dict(
            source=str(path),
            kind="video",
            size=5,
            identity=identity(path),
            status="ready",
            record_start_ms=day + 72000000,
            record_end_ms=day + 72001000,
            record_date="2026-08-17",
            covered_dates=["2026-08-17"],
            metadata={"naming_only": True, "needs_review": True, "intervals": []},
            extension=".mp4",
        )

    monkeypatch.setattr(m, "inspect", read)
    rows = []
    plan = r.plan_import(
        root,
        [{"kind": "auto", "path": str(incoming), "camera": "auto"}],
        scenario="attach_video",
        farm=str(root.parent),
        category="calving",
        cache=tmp_path / "cache",
        fast_video=True,
        on_row=rows.append,
    )
    row = plan["rows"][0]
    assert row["status"] == "ready"
    assert (
        Path(row["target"]).relative_to(root).as_posix()
        == "Video/2026-08-17/视角02/2026-08-17_20-00-00.mp4"
    )
    assert not row.get("reference_cow_ids") and not row.get("cow_id")
    assert len(rows) == 1


def test_nonvideo_deletion_is_explicit_scoped_and_reported(tmp_path):
    from cowmata_tailring.workspace import resource_import as r

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    trash = incoming / "说明.txt"
    trash.write_text("unneeded readme")
    label = incoming / "标签.csv"
    label.write_text("keep")
    source = [{"kind": "video", "path": str(incoming), "camera": "视角01"}]
    plan = r.plan_import(
        tmp_path / "out",
        source,
        category="healthy",
        farm="farm",
        fast_video=True,
        cache=tmp_path / "cache",
    )
    delete = next(row for row in plan["rows"] if row["source"] == str(trash))
    assert delete["operation"] == "delete_nonvideo"
    assert trash.exists(), "preview must not delete files"
    events = []
    result = r.execute(plan, tmp_path / "job", on_row=events.append)
    assert result["deleted"] == 1 and not trash.exists()
    assert label.read_text() == "keep"
    assert any(row["status"] == "deleted" for row in events)


def test_auto_source_never_deletes_nonvideo(tmp_path):
    from cowmata_tailring.workspace import resource_import as r

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    trash = incoming / "readme.txt"
    trash.write_text("keep in mixed source")
    plan = r.plan_import(
        tmp_path / "out",
        [{"kind": "auto", "path": str(incoming), "camera": "auto"}],
        category="healthy",
        farm="farm",
        fast_video=True,
        cache=tmp_path / "cache",
    )
    assert all(row.get("operation") != "delete_nonvideo" for row in plan["rows"])
    assert trash.exists()


def test_remuxed_hikvision_first_frame_uses_pixel_reader_before_general_ocr(monkeypatch):
    m = api()
    from PIL import Image

    class OCR:
        engine = type("Engine", (), {})()

        def _hik_read(self, img, report):
            report.update(success=True, wall_ms=1786924800000)
            return True

        def routing_read(self, *a, **kw):
            raise AssertionError("General OCR must not run after pixel reader success")

    monkeypatch.setattr(m._local, "ocr", OCR(), raising=False)
    result = m.read_clock(Image.new("RGB", (1280, 720)), Path("remux.mp4"), lambda: False)
    assert result["wall_ms"] == 1786924800000
    assert result["method"] == "hik_pixel_first_frame"


def test_naming_only_header_does_not_exclude_unverified_later_playback(tmp_path):
    from cowmata_tailring.workspace.probe import SourceInspector

    inspector = SourceInspector(tmp_path, tmp_path / "meta")
    inspector._resource_records = {
        "one.mp4": {
            "record_start_ms": 1000,
            "record_end_ms": 2000,
            "metadata": {"naming_only": True},
            "timezone_offset_minutes": 480,
        }
    }
    hint = inspector.video_hint(tmp_path / "one.mp4")
    assert hint["end_ms"] is None


def test_changed_nonvideo_is_kept_while_other_files_continue(tmp_path):
    from cowmata_tailring.workspace import resource_import as r

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    first, second = incoming / "first.txt", incoming / "second.txt"
    first.write_text("old")
    second.write_text("delete")
    plan = r.plan_import(
        tmp_path / "out",
        [{"kind": "video", "path": str(incoming), "camera": "视角01"}],
        category="healthy",
        farm="farm",
        fast_video=True,
        cache=tmp_path / "cache",
    )
    first.write_text("changed during review")
    rows = []
    result = r.execute(plan, tmp_path / "job", on_row=rows.append)
    assert first.read_text() == "changed during review"
    assert not second.exists()
    assert result["unresolved"] == 1
    assert any(row["status"] == "blocked" for row in rows)


def test_duplicate_second_is_suffixed_without_overwriting(tmp_path, monkeypatch):
    m = api()
    from cowmata_tailring.workspace import resource_import as r
    from cowmata_tailring.workspace.organization import identity

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("a.mp4", "b.mp4"):
        (incoming / name).write_bytes(name.encode())

    def read(path, *a):
        return dict(
            source=str(path),
            kind="video",
            size=5,
            identity=identity(path),
            status="ready",
            record_start_ms=1786896000000,
            record_end_ms=1786896001000,
            record_date="2026-08-17",
            covered_dates=["2026-08-17"],
            metadata={"naming_only": True, "needs_review": True, "intervals": []},
            extension=".mp4",
        )

    monkeypatch.setattr(m, "inspect", read)
    plan = r.plan_import(
        tmp_path / "out",
        [{"kind": "video", "path": str(incoming), "camera": "视角03"}],
        category="healthy",
        farm="farm",
        fast_video=True,
        cache=tmp_path / "cache",
    )
    assert len({row["target"] for row in plan["rows"]}) == 2
    assert any("__001.mp4" in row["target"] for row in plan["rows"])
    r.execute(plan, tmp_path / "job")
    assert {Path(row["target"]).read_bytes() for row in plan["rows"]} == {b"a.mp4", b"b.mp4"}


def test_failed_nonvideo_hash_cannot_become_delete_ready(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import resource_import as r

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "readme.txt").write_text("unneeded")

    def fail(*a):
        raise OSError("file is locked")

    monkeypatch.setattr(r, "verified_source_digest", fail)
    plan = r.plan_import(
        tmp_path / "out",
        [{"kind": "video", "path": str(incoming), "camera": "视角01"}],
        category="healthy",
        farm="farm",
        fast_video=True,
        cache=tmp_path / "cache",
    )
    assert plan["rows"][0]["status"] == "blocked"
    assert plan["rows"][0].get("operation") != "delete_nonvideo"


def test_execute_button_is_visible_above_results_and_runs_ready_plan(
    organizer, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QApplication

    source = tmp_path / "source"
    source.mkdir()
    job = tmp_path / "job"
    job.mkdir()
    organizer.plan = {
        "id": "preview-task",
        "mode": "import",
        "schema": "cowmata-resources-3.4",
        "fast_video": True,
        "target": str(tmp_path / "target"),
        "sources": [{"kind": "video", "path": str(source)}],
        "rows": [{"source": str(source / "one.mp4"), "status": "ready"}],
        "allow_partial": True,
    }
    organizer.plan_job = job
    organizer.resize(950, 650)
    organizer.render()
    QApplication.processEvents()
    button = getattr(organizer, "execute_top", None)
    assert button is not None, "No execution button beside the preview results"
    assert button.isVisible() and button.isEnabled()
    assert button.geometry().top() < organizer.table.geometry().top()
    requests = []
    monkeypatch.setattr(organizer, "start_job", lambda request, job=None: requests.append(request))
    organizer.execute_top.click()
    assert requests == [{"action": "execute"}]


def test_complete_record_shows_unabridged_paths_and_reason(organizer):
    row = {
        "source": "E:/录像来源/非常长的原始目录/视角01/原始录像.mp4",
        "target": "E:/整理结果/类别/Video/2026-08-17/视角01/2026-08-17_00-00-00.mp4",
        "status": "blocked",
        "message": "首帧日期没有读清，请保留原始文件后核对画面中的完整时间戳。",
    }
    organizer.model.set_rows([row])
    organizer.table.selectRow(0)
    panel = getattr(organizer, "record_details", None)
    assert panel is not None, "Complete record panel is missing"
    content = panel.toPlainText()
    assert row["source"] in content and row["target"] in content and row["message"] in content


def test_one_click_saves_own_open_project_then_executes(organizer, tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    job = tmp_path / "job"
    job.mkdir()
    organizer.plan = {
        "id": "pause-task",
        "mode": "import",
        "schema": "cowmata-resources-3.4",
        "fast_video": True,
        "target": str(source),
        "sources": [{"kind": "video", "path": str(source)}],
        "rows": [{"source": str(source / "one.mp4"), "status": "ready"}],
        "allow_partial": True,
    }
    organizer.plan_job = job
    organizer.owner.catalog = SimpleNamespace(root=source)
    steps = []

    def pause(callback):
        steps.append("saved")
        organizer.owner.catalog = None
        callback("saved and released")

    organizer.owner.pause_for_organization = pause
    monkeypatch.setattr(organizer, "start_job", lambda *args: steps.append("executed"))
    organizer.execute_plan()
    assert steps == ["saved", "executed"]
