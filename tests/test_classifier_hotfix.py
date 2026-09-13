# ruff: noqa: F811 -- imported pytest fixtures are injected by name
import base64
import csv
import json
from pathlib import Path

import pytest
from test_fixes_v351 import organizer  # noqa: F401


def video_row(path, *_):
    from cowmata_tailring.workspace.organization import identity

    return dict(
        source=str(path),
        kind="video",
        status="ready",
        message="",
        identity=identity(path),
        size=path.stat().st_size,
        record_start_ms=1786896000000,
        record_end_ms=1786896001000,
        record_date="2026-08-17",
        covered_dates=["2026-08-17"],
        extension=".mp4",
        metadata={"naming_only": True, "needs_review": True, "intervals": []},
    )


def test_live_csv_is_readable_before_completion_and_contains_elapsed(tmp_path):
    from cowmata_tailring.workspace.organization_live import LiveReport

    live = LiveReport(tmp_path)
    live.row(dict(source="一.mp4", status="processing", file_seconds=1.25))
    with live.path.open(encoding="utf-8-sig", newline="") as reader:
        rows = list(csv.DictReader(reader))
        assert rows[-1]["status"] == "processing"
        live.row(dict(source="一.mp4", status="done", file_seconds=3.5))
    assert list(csv.DictReader(live.path.open(encoding="utf-8-sig")))[-1]["file_seconds"] == "3.5"


def test_new_copy_hashes_during_transfer(tmp_path):
    import hashlib

    from cowmata_tailring.workspace.fast_transfer import copy_verified

    source = tmp_path / "a"
    source.write_bytes(b"abcdef" * 100)
    stats = copy_verified(source, tmp_path / "partial", None, block_size=32)
    assert stats["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_normal_video_outside_selected_dates_is_archived(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v

    monkeypatch.setattr(v, "inspect", video_row)
    source = tmp_path / "incoming/右2/a.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    result = v.organize(
        tmp_path / "farm",
        [dict(kind="video", path=str(source.parent), camera="auto")],
        start="2026-08-20",
        category="calving",
        farm=str(tmp_path / "farm"),
        job=tmp_path / "job",
        cache=tmp_path / "cache",
    )
    assert result["archived_files"] == 1
    assert Path(result["rows"][0]["target"]).parent.name == "视角03"
    assert "2026-08-17" in result["rows"][0]["target"]
    assert source.exists()


def test_unrecognized_camera_keeps_separate_source_identity(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v

    monkeypatch.setattr(v, "inspect", video_row)
    specs = []
    for folder in ("barnA", "barnB"):
        p = tmp_path / folder / "a.mp4"
        p.parent.mkdir()
        p.write_bytes(folder.encode())
        specs.append(dict(kind="video", path=str(p.parent), camera="auto"))
    plan = v.plan_import(
        tmp_path / "farm",
        specs,
        category="calving",
        farm=str(tmp_path / "farm"),
        cache=tmp_path / "cache",
    )
    assert all(r["status"] == "ready" for r in plan["rows"])
    assert len({r["owner"] for r in plan["rows"]}) == 2


def test_cleanup_keeps_json_and_video_and_preserves_ancillary_bytes(tmp_path):
    from cowmata_tailring.workspace.organization_live import clean_modalities

    root, job = tmp_path / "farm", tmp_path / "job"
    job.mkdir()
    for name in [
        "Motion/day/data.json",
        "PPG/day/data.json",
        "Video/day/a.mp4",
        "PPG/占位说明.txt",
        "Video/day/note.csv",
    ]:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"original")
    clean_modalities(root, job)
    assert (root / "Video/day/a.mp4").is_file()
    assert (root / "PPG/day/data.json").is_file()
    assert not (root / "PPG/占位说明.txt").exists()
    assert (root / "归类附属文件/Video/day/note.csv").read_bytes() == b"original"


def test_ppg_json_is_actually_archived(tmp_path):
    from cowmata_tailring.workspace import video_intake as v

    p = tmp_path / "incoming/PPG/data.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            dict(
                device="546C50CA07D5",
                create_time=1786896000000,
                ppg=base64.b64encode(b"1234").decode(),
            )
        )
    )
    result = v.organize(
        tmp_path / "farm",
        [dict(kind="auto", path=str(p.parent), camera="auto")],
        category="calving",
        farm=str(tmp_path / "farm"),
        cache=tmp_path / "cache",
        job=tmp_path / "job",
    )
    assert result["archived_files"] == 1, result
    row = result["rows"][0]
    assert row["kind"] == "ppg" and "PPG" in Path(row["target"]).parts
    assert Path(row["target"]).read_bytes() == p.read_bytes()


def test_resume_button_finds_actual_pending_job_not_empty_last_attempt(
    organizer, tmp_path, monkeypatch
):
    from cowmata_tailring.workspace import organization_live

    job = tmp_path / "original"
    job.mkdir()
    plan = dict(
        mode="import",
        streaming=True,
        rows=[],
        sources=[],
        target=str(tmp_path / "farm"),
        category="calving",
        farm_path=str(tmp_path / "farm"),
        scenario="mixed",
    )
    (job / "plan.json").write_text(json.dumps(plan))
    organizer.settings.setValue("organization/resume_job", str(tmp_path / "empty_attempt"))
    organizer.target.setText(str(tmp_path / "farm"))
    monkeypatch.setattr(organization_live, "pending_job", lambda paths: job)
    calls = []
    monkeypatch.setattr(organizer, "execute_plan", lambda: calls.append(organizer.plan_job))
    organizer.resume_task()
    assert calls == [job]
    assert organizer.job == job


def test_compact_ui_exposes_only_classification_workflow(organizer):
    assert organizer.windowTitle() == "COWMATA · 数据归类"
    assert not organizer.tabs.isVisible()
    assert organizer.execute_top.isVisible()
    assert organizer.cancel_button.isVisible()
    assert organizer.export_button.isVisible()
    assert organizer.sources.isVisible()


def test_inspect_cancellation_is_not_a_bad_video(tmp_path):
    from cowmata_tailring.workspace.video_intake import inspect

    p = tmp_path / "a.mp4"
    p.write_bytes(b"\0\0\1\xba" + b"\0" * 100)
    with pytest.raises(InterruptedError):
        inspect(p, tmp_path / "cache", lambda: True)


@pytest.mark.parametrize("text", ["2026-08-181605.16", "2026-08-1816:0516", "2026-08.181605-16"])
def test_archive_ocr_recovers_punctuation_without_changing_evidence_parser(text):
    from cowmata_tailring.workspace.ocr import parse_stamp
    from cowmata_tailring.workspace.video_intake import parse_archive_stamp

    assert parse_archive_stamp(text) == "2026-08-18 16:05:16"
    assert parse_stamp(text) is None


def test_resume_during_initial_scan_uses_saved_request(organizer, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import organization_live

    job = tmp_path / "scan"
    job.mkdir()
    request = dict(action="organize", target=str(tmp_path / "farm"), sources=[], category="calving")
    (job / "request.json").write_text(json.dumps(request))
    organizer.settings.setValue("organization/resume_job", str(job))
    monkeypatch.setattr(organization_live, "pending_job", lambda paths: None)
    calls = []
    monkeypatch.setattr(
        organizer, "start_organize", lambda request, job: calls.append((request, job))
    )
    organizer.resume_task()
    assert calls == [(request, job)]


def test_multiple_open_excel_snapshots_do_not_interrupt_live_log(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import organization_live as live_module

    live = live_module.LiveReport(tmp_path)
    (tmp_path / "report-live.csv").write_text("locked")
    append = live_module.append_shared

    def locked(path, text):
        if path.name in {"report.csv", "report-live.csv"}:
            raise PermissionError("Excel snapshot locked")
        append(path, text)

    monkeypatch.setattr(live_module, "append_shared", locked)
    live.row(dict(source="video.mp4", status="done"))
    assert live.path.name == "report-live-001.csv"
    assert list(csv.DictReader(live.path.open(encoding="utf-8-sig")))[-1]["status"] == "done"


def test_new_task_reuses_archived_files_without_inspection_hashing_or_copy(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import fast_transfer, resource_import
    from cowmata_tailring.workspace import video_intake as v

    source = tmp_path / "incoming/a.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    monkeypatch.setattr(v, "inspect", video_row)
    args = dict(category="calving", farm=str(tmp_path / "farm"), cache=tmp_path / "cache")
    specs = [dict(kind="video", path=str(source), camera="视角01")]
    v.organize(tmp_path / "farm", specs, job=tmp_path / "job1", **args)

    def forbidden(*a, **kw):
        pytest.fail("Already archived file was processed again")

    monkeypatch.setattr(v, "inspect", forbidden)
    monkeypatch.setattr(resource_import, "verified_source_digest", forbidden)
    monkeypatch.setattr(fast_transfer, "copy_verified", forbidden)
    result = v.organize(tmp_path / "farm", specs, job=tmp_path / "job2", **args)
    assert result["reused_files"] == 1 and result["copied"] == 0
    assert result["rows"][0]["status"] == "done"


def test_missing_archived_target_is_repaired(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v

    source = tmp_path / "incoming/a.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    calls = []

    def inspect(path, *args):
        calls.append(path)
        return video_row(path)

    monkeypatch.setattr(v, "inspect", inspect)
    args = dict(category="calving", farm=str(tmp_path / "farm"), cache=tmp_path / "cache")
    specs = [dict(kind="video", path=str(source), camera="视角01")]
    first = v.organize(tmp_path / "farm", specs, job=tmp_path / "job1", **args)
    Path(first["rows"][0]["target"]).unlink()
    result = v.organize(tmp_path / "farm", specs, job=tmp_path / "job2", **args)
    assert len(calls) == 2 and result["copied"] == 1
    assert Path(result["rows"][0]["target"]).read_bytes() == b"video"


def test_legacy_live_csv_headers_are_never_mixed_with_new_columns(tmp_path):
    from cowmata_tailring.workspace.organization_live import LiveReport
    old = 'source,status\nold.mp4,done\n'
    for name in ('report.csv', 'report-live.csv'):
        (tmp_path / name).write_text(old, encoding='utf-8')
    live = LiveReport(tmp_path)
    live.row(dict(source='new.mp4', status='done', source_folder='folder'))
    assert live.path.name == 'report-live-001.csv'
    assert (tmp_path / 'report-live.csv').read_text(encoding='utf-8') == old
    assert list(csv.DictReader(live.path.open(encoding='utf-8-sig')))[-1]['source_folder'] == 'folder'
