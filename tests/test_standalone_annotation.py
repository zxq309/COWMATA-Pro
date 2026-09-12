import json


def test_single_imu_scans_only_that_record_and_its_video_day(tmp_path):
    from test_resources_v34 import record

    from cowmata_tailring.workspace.standalone import StandaloneCatalog

    root = tmp_path / "牧场/产犊"
    raw = record(root / "Motion/2026-08-03")
    record(root / "Motion/2026-08-03/other")
    movie = root / "Video/2026-08-03/视角01/one.mp4"
    movie.parent.mkdir(parents=True)
    movie.write_bytes(b"video")
    (root / "PPG").mkdir()
    cat = StandaloneCatalog(raw, day="2026-08-03", meta_path=tmp_path / "scratch")
    cat.scan(fast=True)
    assert len(cat.rows(kind="imu")) == 1 and len(cat.rows(kind="video")) == 1
    assert not (root / "标注工程").exists()
    cat.close()


def test_explicit_save_always_chooses_path_and_cancel_close_preserves_work(tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QApplication, QFileDialog
    from test_resources_v34 import record

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / "牧场/产犊"
    raw = record(root / "Motion/2026-08-03")
    video = root / "Video/2026-08-03"
    video.mkdir(parents=True)
    (root / "PPG").mkdir()
    before = raw.read_bytes()
    output = tmp_path / "customer-choice/labels.标注.json"
    calls = []
    answers = [str(output), str(output), ""]

    def choose(*args, **kwargs):
        calls.append(args[1])
        return answers.pop(0), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    window = MainWindow()
    window.open_standalone(raw, video_directory=video)
    deadline = time.monotonic() + 10
    while window.work is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.work is not None
    assert window.save_user_annotations(),window.statusBar().currentMessage()
    assert window.save_user_annotations()
    assert len(calls) == 2 and output.exists() and raw.read_bytes() == before
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["format"] == "cowmata-annotation" and saved["version"] == 1
    start = window.work.clock.map(0)
    window.work.add_draft(0, start, start + 20, [])
    window.confirm_close = lambda: "save"
    window.close()
    app.processEvents()
    assert len(calls) == 3 and not window._closing_requested and window.work.drafts
    window._close_choice = "discard"
    window.close()
    deadline = time.monotonic() + 10
    while not window._closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._closed and not (root / "标注工程").exists()


def test_single_video_can_pair_external_imu_and_saved_label_reopens_both(tmp_path):
    import hashlib

    from test_resources_v34 import record

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.workspace.clocks import ClockMap
    from cowmata_tailring.workspace.label_file import load_history, save_label_file
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.standalone import StandaloneCatalog, standalone_document
    from cowmata_tailring.workspace.work import SessionWork

    raw = record(tmp_path / "raw")
    movie = tmp_path / "separate-volume/one.mp4"
    movie.parent.mkdir()
    movie.write_bytes(b"video")
    motion = load_motion_json(raw)
    cat = StandaloneCatalog(
        raw, video=movie, day="2026-08-03", meta_path=tmp_path / "scratch", stability_seconds=0
    )
    cat.scan(fast=True)
    raw_row = cat.rows(kind="imu")[0]
    cat.index_one(raw_row["path"], SourceInspector(cat.root, cat.meta), eager=True)
    video_row = cat.rows(kind="video")[0]
    session = SessionWork(hashlib.sha256(raw.read_bytes()).hexdigest())
    session.clock = ClockMap.from_capture(motion)
    start = session.clock.map(0)
    metadata = dict(
        camera="视角01",
        duration_ms=1000,
        needs_review=False,
        intervals=[
            dict(
                wall_start=start,
                wall_end=start + 1000,
                media_start=0,
                media_end=1000,
                verified=True,
            )
        ],
    )
    cat.index_one(video_row["path"], lambda *_: metadata, eager=True)
    doc = standalone_document(cat, session, motion, {}, cat.rows())
    target = tmp_path / "chosen-location/result.标注.json"
    save_label_file(target, doc)
    loaded = load_history(target)
    assert loaded.motion and len(loaded.rows) == 1
    assert loaded.source_path(loaded.rows[0]["path"]) == movie
    assert len(cat.rows(kind="imu")) == 1 and len(cat.rows(kind="video")) == 1
    cat.close()
