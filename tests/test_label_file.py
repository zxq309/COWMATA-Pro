import base64
import copy
import shutil
import struct
import time

import numpy as np
import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.label_file import (
    build_label_file,
    load_history,
    read_label_file,
    save_label_file,
)
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    times = [0, 20, 40, 60, 80, 100, 320, 340, 360, 380, 400]
    raw = b"".join(struct.pack("<I9h", t + 100000, *([i + 1] * 9)) for i, t in enumerate(times))
    path = root / "same-name.json"
    atomic_json(path, {"imu": base64.b64encode(raw).decode(), "version": 2, "device": "device-A", "create_time": 1785732300000})
    motion = load_motion_json(path)
    work = SessionWork(digest_file(path))
    work.project.source = {"path": path.name, "asset_id": work.asset_id, "durationMs": 400}
    work.project.cow_id = "TEST-COW"
    work.clock = ClockMap([Anchor(0, 10000), Anchor(400, 10400)])
    event = work.project.add_event(0, 60, 360)
    event.extras["confirmation"] = "needs_review"
    (root / "001.mp4").write_bytes(b"FAKE-VIDEO-CONTENT-DO-NOT-DECODE")
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(now=1)
    catalog.index_one(path.name, lambda *_: {}, now=2)
    catalog.index_one("001.mp4", lambda *_: {"camera": "A", "intervals": [{"wall_start": 10000, "wall_end": 11000, "media_start": 0, "media_end": 1000, "verified": True}]}, now=2)
    rows = catalog.rows()
    catalog.close()
    return root, motion, work, rows


def document(source, **kw):
    root, motion, work, rows = source
    return build_label_file(work, motion, root, rows, {"selected_cameras": ["A"]}, **kw)


def test_one_file_flat_roundtrip_and_no_source_writes(source, tmp_path):
    root, motion, work, _ = source
    before = {p: digest_file(p) for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "anywhere" / "label.json"
    save_label_file(output, document(source))
    loaded = load_history(output)
    assert loaded.work.to_dict() == work.to_dict()
    assert loaded.motion.sample_count == motion.sample_count
    assert loaded.timeline.locate("A", 10340)[1] == 340
    assert list(output.parent.iterdir()) == [output]
    assert {p: digest_file(p) for p in before} == before


def test_full_export_contains_exact_original_and_opens_without_project(source, tmp_path):
    root, motion, work, _ = source
    original = motion.source_path.read_bytes()
    doc = document(source)
    assert doc["version"] == 2
    assert base64.b64decode(doc["embedded_imu"]["original_json_base64"]) == original
    assert doc["embedded_imu"]["sha256"] == work.asset_id
    doc["source"]["project_root_hint"] = ""
    output = tmp_path / "with-record.json"
    save_label_file(output, doc)
    result = load_history(output)
    assert result.root is None
    np.testing.assert_array_equal(result.motion.times_ms, motion.times_ms)
    np.testing.assert_array_equal(result.motion.channels["ax"], motion.channels["ax"])
    assert result.work.to_dict() == work.to_dict()
    assert not result.timeline.intervals  # video is never embedded


@pytest.mark.parametrize("kind", ["changed_source", "corrupt_blob", "wrong_identity"])
def test_full_record_identity_is_verified(source, tmp_path, kind):
    doc = document(source)
    if kind == "changed_source":
        source[1].source_path.write_bytes(b"changed recording")
        with pytest.raises(ValueError, match="source changed"):
            document(source)
        return
    if kind == "corrupt_blob":
        doc["embedded_imu"]["original_json_base64"] = base64.b64encode(b"{}").decode()
    else:
        doc["embedded_imu"]["sha256"] = "b" * 64
    output = tmp_path / "corrupt.json"
    atomic_json(output, doc)
    with pytest.raises(ValueError, match="identity"):
        load_history(output)


@pytest.mark.parametrize("version", [0, 1, 2])
def test_snippet_keeps_exact_frames_times_and_parent_offset(source, tmp_path, version):
    root, motion, work, rows = source
    if version != 2:
        raw = b"".join((struct.pack("<H", delta) if version == 1 else b"") + struct.pack("<9h", *([i + 1] * 9))
                       for i, delta in enumerate([20, 20, 20, 20, 20, 20, 220, 20, 20, 20, 20]))
        atomic_json(motion.source_path, {"imu": base64.b64encode(raw).decode(), "version": version, "create_time": motion.create_time_ms})
        motion = load_motion_json(motion.source_path)
        work.asset_id = digest_file(motion.source_path)
        source = (root, motion, work, rows)
    lo, hi = motion.times_ms[3], motion.times_ms[8]
    doc = document(source, selection=(lo + 1, hi))
    output = tmp_path / "snippet.json"
    save_label_file(output, doc)
    # Prove a snippet still opens without the original project/IMU/index.
    doc["source"]["project_root_hint"] = ""
    atomic_json(output, doc)
    result = load_history(output)
    np.testing.assert_array_equal(result.motion.times_ms, motion.times_ms[4:9])
    for key in ("ax", "gy", "mz"):
        np.testing.assert_array_equal(result.motion.channels[key], motion.channels[key][4:9])
    assert result.work.project.events[0].t0 == 60  # no clipped truth boundary
    assert result.work.clock.map(result.motion.times_ms[0]) == 10000 + lo + 20
    assert result.motion.gap_count == (1 if version else 0)


def test_move_project_and_labels_independently(source, tmp_path):
    root, _, work, rows = source
    output = tmp_path / "loose.json"
    save_label_file(output, document(source, include_record=False))
    relocated = tmp_path / "another-disk"
    shutil.copytree(root, relocated)
    # Same basename deliberately points to the wrong IMU in old root.
    atomic_json(root / "same-name.json", {"imu": "wrong"})
    loaded = load_history(output, relocated)
    assert loaded.motion.source_path.parent == relocated
    assert loaded.work.asset_id == work.asset_id
    assert loaded.timeline.locate("A", 10100)[1] == 100
    assert load_history(output).motion is None
    (relocated / "001.mp4").write_bytes(b"different video with same filename")
    assert not load_history(output, relocated).timeline.intervals


def test_renamed_sources_found_by_indexed_hash(source, tmp_path):
    root, _, _, rows = source
    output = tmp_path / "loose.json"
    save_label_file(output, document(source, include_record=False))
    for old, new in (("same-name.json", "renamed.json"), ("001.mp4", "renamed.mp4")):
        (root / old).rename(root / new)
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(now=100)
    for row in rows:
        catalog.index_one("renamed.json" if row["kind"] == "imu" else "renamed.mp4", lambda *_, r=row: r["metadata"], now=101)
    catalog.close()
    result = load_history(output)
    assert result.motion.source_path.name == "renamed.json"
    assert result.timeline.locate("A", 10100)[0].path == "renamed.mp4"


def test_missing_index_uses_verified_snapshot_without_creating_directories(source, tmp_path):
    root, _, _, _ = source
    output = tmp_path / "loose.json"
    save_label_file(output, document(source))
    root.joinpath("标注工程", "index.sqlite").rename(root / "archived-index")
    before = set(root.rglob("*"))
    result = load_history(output)
    assert result.timeline.locate("A", 10100)[1] == 100
    assert set(root.rglob("*")) == before


def test_corrupt_index_falls_back_readonly(source, tmp_path):
    root, _, _, _ = source
    output = tmp_path / "loose.json"
    save_label_file(output, document(source))
    index = root / "标注工程" / "index.sqlite"
    index.write_bytes(b"CORRUPT INDEX")
    result = load_history(output)
    assert index.read_bytes() == b"CORRUPT INDEX"
    assert result.motion is not None
    assert result.timeline.locate("A", 10100)[1] == 100
    assert any("索引" in w for w in result.warnings)


def test_historical_camera_mapping_is_not_silently_changed(source, tmp_path):
    root, motion, work, rows = source
    old = ClockMap([Anchor(10000, 10000), Anchor(11000, 11000)])
    settings = {"camera_maps": {"A": old.to_dict()}}
    doc = build_label_file(work, motion, root, rows, settings)
    output = tmp_path / "label.json"
    save_label_file(output, doc)
    new = ClockMap([Anchor(10000, 30000), Anchor(11000, 31000)])
    atomic_json(root / "标注工程" / "project.json", {"camera_maps": {"A": new.to_dict()}})
    result = load_history(output)
    assert result.timeline.locate("A", 10100)[1] == 100
    assert result.timeline.camera_maps["A"].revision == old.revision
    assert any("校准版本" in w for w in result.warnings)


@pytest.mark.parametrize("kind", ["raw", "different_work", "protected", "unrelated_list"])
def test_cannot_overwrite_sources_or_other_annotations(source, tmp_path, kind):
    root, motion, _, _ = source
    doc = document(source)
    if kind == "raw":
        path = motion.source_path
    elif kind == "unrelated_list":
        path = tmp_path / "other.json"
        atomic_json(path, ["not an annotation"])
    else:
        path = tmp_path / "label.json"
        other = copy.deepcopy(doc)
        if kind == "different_work":
            other["source"]["asset_id"] = "b" * 64
        save_label_file(path, other)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_label_file(path, doc, protected=[path] if kind == "protected" else [])
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["parent_identity", "offset", "payload", "version", "coordinates"])
def test_reject_invalid_document(source, tmp_path, kind):
    doc = document(source, selection=(80, 360))
    if kind == "parent_identity":
        doc["source"]["asset_id"] = "wrong"
    elif kind == "offset":
        doc["embedded_imu"]["parent_start_ms"] += 20
    elif kind == "payload":
        doc["embedded_imu"]["frames_sha256"] = "wrong"
    else:
        doc[kind] = "future-version"
    path = tmp_path / "bad.json"
    atomic_json(path, doc)
    with pytest.raises(ValueError):
        load_history(path)


def test_legacy_formats_and_unknown_alignment_are_not_guessed(source, tmp_path):
    _, _, work, _ = source
    work.clock = ClockMap()
    for name, data in (("workspace", work.to_dict()), ("old-single", work.project.to_dict())):
        path = tmp_path / (name + ".json")
        atomic_json(path, data)
        result = load_history(path, source[0])
        assert len(result.work.project.events) == 1
        assert not result.work.clock.anchors
        assert not result.timeline.intervals
        assert any("服务器" in w for w in result.warnings)


def test_export_ui_is_one_file_not_a_batch_directory(source, tmp_path, monkeypatch):
    from cowmata_tailring.workspace.modern_window import MainWindow
    app = QApplication.instance() or QApplication([])
    root, motion, work, rows = source
    window = MainWindow()
    for timer in (window.board.timer, window.save_timer, window.source_timer):
        timer.stop()
    window.catalog = Catalog(root)
    window.motion, window.work, window.rows = motion, work, rows
    output = tmp_path / "label.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kw: (str(output), "JSON"))
    window.export_work()
    deadline = time.monotonic() + 5
    while window._export_running and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert not window._export_running
    assert read_label_file(output)["work"]["asset_id"] == work.asset_id
    assert not list(tmp_path.glob("COWMATA*"))
    window.close()
    window.catalog.close()


def test_history_gui_readonly_bounds_and_close(source, tmp_path):
    from cowmata_tailring.workspace.history_window import HistoryWindow
    app = QApplication.instance() or QApplication([])
    output = tmp_path / "history.json"
    doc = document(source, selection=(80, 360))
    doc["video"]["rows"] = []
    doc["source"]["project_root_hint"] = ""
    save_label_file(output, doc)
    before = output.read_bytes()
    window = HistoryWindow(output)
    window.board.timer.stop()
    window.show()
    deadline = time.monotonic() + 5
    while window.future and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert window.data and window.data.motion.sample_count == 5
    assert not window.plot.wave.event_editable
    window.review_event(window.data.work.project.events[0].id)
    assert window.slider.value() == 0  # event starts before visible snippet
    window.seek(340)
    assert window.board.reference_ms == 10340
    window.board.playing = True
    window.video_time(10400)
    assert not window.board.playing and window.board.reference_ms == 10360
    window.close()
    assert output.read_bytes() == before


def test_history_close_reopen_reuses_one_bounded_video_pool(source, tmp_path):
    from cowmata_tailring.workspace.history_window import HistoryWindow
    app = QApplication.instance() or QApplication([])
    output = tmp_path / "history.json"
    doc = document(source, selection=(80, 360))
    doc["video"]["rows"] = []
    doc["source"]["project_root_hint"] = ""
    save_label_file(output, doc)
    window = HistoryWindow(output, reusable=True)
    for cycle in range(3):
        if cycle:
            window.begin_load(None)
        window.future.result(timeout=5)
        window.poll_load()
        window.board.select([str(i) for i in range(8)])  # no media: surfaces only
        window.show()
        app.processEvents()
        handles = [int(t.surface.winId()) for t in window.board.pool]
        if cycle == 0:
            original = handles
        assert handles == original and len(window.board.pool) == 8
        window.close()
        assert window.closed and not window.disposed and not window.board.playing
    window.dispose()
    assert window.disposed and window.loader._shutdown
