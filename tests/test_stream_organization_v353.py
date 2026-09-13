# ruff: noqa: F811 -- pytest fixture injection
import json
import threading
from pathlib import Path

import pytest
from test_fixes_v351 import organizer  # noqa: F401


@pytest.mark.parametrize("scenario", ["mixed", "attach_video"])
@pytest.mark.parametrize("transfer", ["copy", "move"])
def test_first_video_reaches_disk_while_next_is_still_recognizing(
    tmp_path, monkeypatch, scenario, transfer
):
    from test_resources_v34 import record

    from cowmata_tailring.workspace import video_intake as v
    from cowmata_tailring.workspace.organization import identity

    root = tmp_path / "farm/产犊"
    reference = []
    if scenario == "attach_video":
        raw = record(root / "Motion")
        reference = [
            dict(
                kind="imu",
                path=raw.relative_to(root).as_posix(),
                size=raw.stat().st_size,
                record_start_ms=1786896000000,
                record_end_ms=1786896001000,
            )
        ]
        (root / "资源索引.json").write_text(
            json.dumps({"dataset_category": "calving", "records": reference})
        )
    fast, slow = tmp_path / "view1/a.mp4", tmp_path / "view2/b.mp4"
    for path in (fast, slow):
        path.parent.mkdir()
        path.write_bytes(path.name.encode())
    entered = threading.Event()
    release = threading.Event()
    events = []

    def inspect(path, *_):
        if path == slow:
            entered.set()
            assert release.wait(8), "First file was not transferred until all recognition finished"
        return dict(
            source=str(path),
            kind="video",
            status="ready",
            identity=identity(path),
            size=path.stat().st_size,
            record_start_ms=1786896000000,
            record_end_ms=1786896001000,
            record_date="2026-08-17",
            covered_dates=["2026-08-17"],
            extension=".mp4",
            metadata={"naming_only": True, "needs_review": True, "intervals": []},
        )

    monkeypatch.setattr(v, "inspect", inspect)

    def event(row):
        events.append(row)
        assert row["status"] != "blocked", row.get("message")
        if row["status"] == "done" and row["source"] == str(fast):
            target = Path(row["target"])
            assert entered.wait(1)
            assert target.read_bytes() == b"a.mp4"
            assert fast.exists() == (transfer == "copy")
            index = json.loads((root / "资源索引.json").read_text(encoding="utf-8"))
            assert any(x["path"] == target.relative_to(root).as_posix() for x in index["records"])
            release.set()

    run = getattr(v, "organize", None)
    assert callable(run), "Streaming organization is missing"
    try:
        result = run(
            root,
            [
                dict(kind="video", path=str(fast.parent), camera="视角01"),
                dict(kind="video", path=str(slow.parent), camera="视角02"),
            ],
            category="calving",
            farm=str(root.parent),
            scenario=scenario,
            transfer=transfer,
            workers=2,
            cache=tmp_path / "cache",
            job=tmp_path / "job",
            on_row=event,
        )
    finally:
        release.set()
    assert result["completed"] and result["moved"] == 2
    assert sum(x["status"] == "done" for x in events) == 2
    assert {Path(x["target"]).parent.name for x in events if x["status"] == "done"} == {
        "视角01",
        "视角02",
    }


def test_one_click_starts_transfer_without_waiting_for_preview(organizer, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    source = tmp_path / "video"
    source.mkdir()
    organizer.target.setText(str(tmp_path / "target"))
    organizer.category.setCurrentIndex(organizer.category.findData("healthy"))
    organizer.add_source("video", source, "视角02")
    organizer.render()
    QApplication.processEvents()
    assert organizer.execute_top.isEnabled(), "Execution still requires a completed preview"
    requests = []
    monkeypatch.setattr(organizer, "start_job", lambda request, job=None: requests.append(request))
    organizer.execute_top.click()
    assert requests[0]["action"] == "organize"
    assert requests[0]["sources"][0]["camera"] == "视角02"


def test_bulk_view_directories_keep_independent_camera_mapping(tmp_path):
    from cowmata_tailring.workspace import organization_ui as ui

    cls = getattr(ui, "VideoDirectoriesDialog", None)
    assert cls is not None, "Bulk view directory entry is missing"
    dialog = cls()
    try:
        for view in ("视角01", "视角02", "视角08"):
            folder = tmp_path / view
            folder.mkdir()
            dialog.paths[view].setText(str(folder))
        specs = dialog.source_specs()
        assert [(s["camera"], Path(s["path"]).name) for s in specs] == [
            ("视角01", "视角01"),
            ("视角02", "视角02"),
            ("视角08", "视角08"),
        ]
    finally:
        dialog.close()


def test_video_intake_filters_suffix_before_opening_unrelated_files(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import video_intake as v

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("notes.txt", "image.jpg", "index.csv", "unknown.bin"):
        (incoming / name).write_bytes(b"not video")
    monkeypatch.setattr(
        v, "inspect", lambda *a: pytest.fail("Non-video suffix reached media processing")
    )
    result = v.organize(
        tmp_path / "out",
        [dict(kind="video", path=str(incoming), camera="视角02")],
        category="healthy",
        farm="farm",
        cache=tmp_path / "cache",
        job=tmp_path / "job",
    )
    assert result["rows"] == []
    assert all(p.exists() for p in incoming.iterdir())


def test_native_opening_clock_avoids_ocr_and_neighbor_frames(tmp_path, monkeypatch):
    from PIL import Image

    from cowmata_tailring.workspace import video_intake as v

    native = getattr(v, "native_first_start", None)
    assert callable(native), "Bounded native opening clock reader is missing"
    path = tmp_path / "one.mp4"
    path.write_bytes(b"\x00\x00\x01\xba" + b"x" * 100)
    monkeypatch.setattr(
        v,
        "probe",
        lambda *a: dict(streams=[dict(codec_type="video")], format=dict(format_name="mpeg")),
    )
    monkeypatch.setattr(
        v, "native_first_start", lambda *a: dict(start_ms=1786924800000, family="hikvision-hk1")
    )
    monkeypatch.setattr(v, "opening_frame", lambda *a: (Image.new("RGB", (100, 100)), 0))
    monkeypatch.setattr(
        v, "read_clock", lambda *a, **kw: pytest.fail("Native time unnecessarily invoked OCR")
    )
    row = v.inspect(path, tmp_path / "cache")
    assert row["status"] == "ready"
    assert row["metadata"]["archive_time"]["basis"] == "native_first_frame"


def test_failed_first_frame_reads_neighbor_and_subtracts_media_offset(tmp_path, monkeypatch):
    from PIL import Image

    from cowmata_tailring.workspace import video_intake as v

    path = tmp_path / "one.mp4"
    path.write_bytes(b"\x00\x00\x01\xba" + b"x" * 100)
    monkeypatch.setattr(
        v,
        "probe",
        lambda *a: dict(streams=[dict(codec_type="video")], format=dict(format_name="mpeg")),
    )
    monkeypatch.setattr(v, "native_first_start", lambda *a: None, raising=False)
    calls = []

    def frame(path, at, *a):
        calls.append(at)
        return Image.new("RGB", (100, 100)), 40 if not at else at

    monkeypatch.setattr(v, "opening_frame", frame)

    def clock(*a, **kw):
        return (
            dict(success=False, wall_ms=None)
            if len(calls) == 1
            else dict(success=True, wall_ms=1786924801000)
        )

    monkeypatch.setattr(v, "read_clock", clock)
    row = v.inspect(path, tmp_path / "cache")
    assert row["status"] == "ready", row
    assert calls == [0, 1040]
    assert row["metadata"]["archive_time"]["start_ms"] == 1786924800000
    assert row["metadata"]["archive_time"]["basis"] == "opening_ocr_estimate"


def test_native_start_is_read_from_real_packet_header_not_tail(tmp_path):
    from test_native_ps import PACK, hk, pes

    from cowmata_tailring.workspace.video_intake import native_first_start

    path = tmp_path / "packet.mp4"
    with path.open("wb") as stream:
        stream.write(PACK + hk("2026-08-17 00:00:00") + pes(0))
        stream.seek(8 * 1024 * 1024)
        stream.write(b"invalid trailer")
    assert native_first_start(path)["start_ms"] == 1786924800000


@pytest.mark.parametrize("transfer", ["copy", "move"])
def test_stream_pause_resume_keeps_real_completed_files(tmp_path, monkeypatch, transfer):
    from cowmata_tailring.workspace import video_intake as v
    from cowmata_tailring.workspace.organization import identity

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("a.mp4", "b.mp4"):
        (incoming / name).write_bytes(name.encode())

    def read(path, *a):
        return dict(
            source=str(path),
            kind="video",
            status="ready",
            identity=identity(path),
            size=path.stat().st_size,
            record_start_ms=1786896000000,
            record_end_ms=1786896001000,
            record_date="2026-08-17",
            covered_dates=["2026-08-17"],
            extension=".mp4",
            metadata={"naming_only": True, "needs_review": True, "intervals": []},
        )

    monkeypatch.setattr(v, "inspect", read)
    stopped = threading.Event()
    seen = []

    def stop_after_first(row):
        if row["status"] == "done":
            seen.append(row)
            stopped.set()

    options = dict(
        category="healthy",
        farm="farm",
        cache=tmp_path / "cache",
        job=tmp_path / "job",
        transfer=transfer,
    )
    specs = [dict(kind="video", path=str(incoming), camera="视角08")]
    with pytest.raises(InterruptedError):
        v.organize(
            tmp_path / "out", specs, cancelled=stopped.is_set, on_row=stop_after_first, **options
        )
    assert len(seen) == 1 and Path(seen[0]["target"]).is_file()
    result = v.organize(tmp_path / "out", specs, **options)
    targets = list((tmp_path / "out").rglob("*.mp4"))
    assert len(targets) == 2 and {p.read_bytes() for p in targets} == {b"a.mp4", b"b.mp4"}
    assert result["archived_files"] == 2


def test_adding_eight_sources_does_not_render_partially_built_rows(
    organizer, tmp_path, monkeypatch
):
    import sys

    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, trace: errors.append(str(error)))
    for number in range(1, 9):
        directory = tmp_path / f"camera{number}"
        directory.mkdir()
        organizer.add_source("video", directory, f"视角{number:02d}")
    assert not errors, errors
    assert len(organizer.source_specs()) == 8


def test_open_actual_destination_never_opens_an_unwritten_preview(organizer, tmp_path, monkeypatch):
    from cowmata_tailring.workspace.organization_ui import QDesktopServices

    target = tmp_path / "out/2026-08-17/视角02/video.mp4"
    target.parent.mkdir(parents=True)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    row = dict(source=str(tmp_path / "source.mp4"), target=str(target), status="ready")
    organizer.model.set_rows([row])
    organizer.table.selectRow(0)
    organizer.open_directory_button.click()
    assert opened == []
    target.write_bytes(b"copied video")
    organizer.model.update_row({**row, "status": "done"})
    organizer.open_directory_button.click()
    assert [Path(p) for p in opened] == [target.parent]
