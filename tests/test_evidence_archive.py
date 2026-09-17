import copy
import hashlib
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_label_file import source  # noqa: F401 - shared real-format synthetic IMU fixture

from cowmata_tailring.workspace.archive import verify_archive
from cowmata_tailring.workspace.catalog import META_DIR, digest_file, file_stamp
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.evidence import (
    capture_frames,
    context_matches,
    event_context,
    evidence_summary,
    read_image,
    safe_relative,
    store_bundle,
    suggest_time,
)
from cowmata_tailring.workspace.label_file import build_label_file, load_history, save_label_file
from cowmata_tailring.workspace.storage import atomic_json


@pytest.fixture
def views(source):  # noqa: F811 - pytest fixture injection
    root, motion, work, rows = source
    video = next(r for r in rows if r["kind"] == "video")
    rows = [r for r in rows if r["kind"] != "video"]
    for index in range(8):
        path = root / f"camera{index}" / "001.mp4"
        path.parent.mkdir()
        path.write_bytes(b"SYNTHETIC-VIDEO-" + str(index).encode())
        row = copy.deepcopy(video)
        row.update(path=path.relative_to(root).as_posix(), asset_id=digest_file(path), stamp=file_stamp(path))
        row["metadata"]["camera"] = str(index)
        rows.append(row)
    event = work.project.events[0]
    event.extras.update(confirmation="confirmed", mapping_revision=work.clock.revision,
                        video_evidence=[{"frame_ready": True, "verified_interval": True}])
    return root, motion, work, rows


def fake_frame(path, target, timeline, **kwargs):
    assert kwargs["image_codec"] == "bmp" and "preview_width" not in kwargs
    return Image.new("RGB", (320, 180), (int(Path(path).parent.name[-1:]) * 30, 100, 150)), target + 10


def capture(views, **kwargs):
    root, motion, work, rows = views
    return capture_frames(root, rows, {}, work.clock, event_context(work, work.project.events[0]),
                          100, list(map(str, range(8))), extractor=kwargs.pop("extractor", fake_frame), **kwargs)


def test_one_per_camera_same_reference_original_frames_and_bounded_decoding(views):
    active = maximum = 0
    lock = threading.Lock()
    def extractor(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
        time.sleep(.01)
        result = fake_frame(*args, **kwargs)
        with lock:
            active -= 1
        return result
    bundle, blobs = capture(views, extractor=extractor)
    assert len(bundle["items"]) == 8 and len(blobs) == 8
    assert {i["requested_reference_ms"] for i in bundle["items"]} == {10100}
    assert all(i["status"] == "captured" and i["delta_ms"] == 10 for i in bundle["items"])
    assert maximum == 2
    assert not bundle["algorithm_input"] and not bundle["human_checked"]


@pytest.mark.parametrize("failure", ["missing", "changing", "far_frame", "unverified", "gap"])
def test_unavailable_view_never_gets_a_fabricated_picture(views, failure):
    root, _, _, rows = views
    row = next(r for r in rows if r["metadata"].get("camera") == "3")
    if failure == "missing":
        (root / row["path"]).unlink()
    elif failure == "changing":
        (root / row["path"]).write_bytes(b"changed")
    elif failure == "unverified":
        row["metadata"]["intervals"][0]["verified"] = False
    elif failure == "gap":
        row["metadata"]["intervals"] = []
    def extractor(path, target, timeline, **kw):
        frame, actual = fake_frame(path, target, timeline, **kw)
        return frame, actual + (1000 if failure == "far_frame" and path.parent.name == "camera3" else 0)
    bundle, blobs = capture(views, extractor=extractor)
    assert bundle["items"][3]["status"] == "unavailable"
    assert len(blobs) == 7 and "path" not in bundle["items"][3]


def test_individual_camera_clock_is_applied(views):
    root, _, work, rows = views
    camera = ClockMap([Anchor(9000, 10000), Anchor(10000, 11000)])
    shifted = copy.deepcopy(rows)
    for row in shifted:
        if row["kind"] == "video":
            for interval in row["metadata"]["intervals"]:
                interval["wall_start"] -= 1000
                interval["wall_end"] -= 1000
    bundle, _ = capture_frames(root, shifted, {"camera_maps": {"0": camera.to_dict()}}, work.clock,
                                event_context(work, work.project.events[0]), 100, ["0"], extractor=fake_frame)
    assert bundle["items"][0]["requested_media_ms"] == 100
    assert bundle["items"][0]["reference_ms"] == 10110


@pytest.mark.parametrize("kind", ["time", "uncalibrated", "cow", "cancel"])
def test_capture_requires_real_review_context(views, kind):
    root, _, work, rows = views
    context = event_context(work, work.project.events[0])
    when = 0 if kind == "time" else 100
    if kind == "uncalibrated":
        work.clock = ClockMap([Anchor(0, 10000)], basis="device_clock")
    if kind == "cow":
        context["cow_id"] = ""
    with pytest.raises((ValueError, InterruptedError)):
        capture_frames(root, rows, {}, work.clock, context, when, ["0"], extractor=fake_frame,
                       cancelled=lambda: kind == "cancel")


def test_move_labels_images_then_offline_history_and_training_isolation(views, tmp_path):
    root, motion, work, rows = views
    bundle, blobs = capture(views)
    bundle["human_checked"] = True
    work.project.events[0].extras["screenshots"] = store_bundle(root / META_DIR, bundle, blobs)
    original = motion.source_path.read_bytes()
    doc = build_label_file(work, motion, root, rows, {})
    output = tmp_path / "成果" / "任意名字.json"
    save_label_file(output, doc, evidence_root=root / META_DIR)
    assert {p.name for p in output.parent.iterdir()} == {"任意名字.json", "证据"}
    assert len(list((output.parent / "证据").iterdir())) == 8
    elsewhere = tmp_path / "离线回看"
    shutil.copytree(output.parent, elsewhere)
    doc["source"]["project_root_hint"] = ""
    atomic_json(elsewhere / output.name, doc)
    loaded = load_history(elsewhere / output.name)
    assert loaded.root is None and not loaded.timeline.intervals
    assert loaded.motion.sample_count == motion.sample_count
    assert evidence_summary(doc, elsewhere)["saved"] == 8
    assert context_matches(loaded.work.project.events[0].extras["screenshots"], loaded.work, loaded.work.project.events[0])
    assert "screenshots" not in work.training_project().events[0].extras
    assert "screenshots" in work.project.events[0].extras
    assert original == motion.source_path.read_bytes()


def test_corrupt_or_missing_images_do_not_destroy_labels_and_export_is_atomic(views, tmp_path):
    root, motion, work, rows = views
    bundle, blobs = capture(views)
    work.project.events[0].extras["screenshots"] = store_bundle(root / META_DIR, bundle, blobs)
    doc = build_label_file(work, motion, root, rows, {})
    output = tmp_path / "export" / "labels.json"
    save_label_file(output, doc, evidence_root=root / META_DIR)
    first = bundle["items"][0]
    (output.parent / first["path"]).write_bytes(b"damaged")
    loaded = load_history(output)
    assert loaded.work.project.events and any("损坏" in w for w in loaded.warnings)
    before = output.read_bytes()
    with pytest.raises(ValueError):
        save_label_file(output, doc, evidence_root=root / META_DIR)
    assert output.read_bytes() == before
    (root / META_DIR / first["path"]).unlink()
    with pytest.raises(OSError):
        save_label_file(output, doc, evidence_root=root / META_DIR)


@pytest.mark.parametrize("relative", ["../outside.jpg", "C:/temp/x.jpg", "/tmp/x.jpg", "dir/../x.jpg", "x:stream.jpg"])
def test_evidence_path_cannot_escape(tmp_path, relative):
    with pytest.raises(ValueError):
        safe_relative(tmp_path, relative)


def test_recalibration_or_boundary_change_marks_old_screenshot_context(views):
    _, _, work, _ = views
    bundle, _ = capture(views)
    assert context_matches(bundle, work, work.project.events[0])
    work.project.events[0].t0 += 20
    assert not context_matches(bundle, work, work.project.events[0])


def test_representative_point_is_inside_label_and_does_not_change_raw(views):
    _, motion, _, _ = views
    before = motion.channels["ax"].copy()
    assert 60 <= suggest_time(motion, 60, 360) <= 360
    assert suggest_time(motion, 60, None) == 60
    np.testing.assert_array_equal(before, motion.channels["ax"])


def test_archive_full_hash_renamed_files_relink_without_index_and_never_delete(views, tmp_path):
    root, motion, work, rows = views
    # The original fixture also has a ninth video; include it in the snapshot.
    old = next(p for p in root.iterdir() if p.suffix == ".mp4")
    old.unlink()  # Only this test-created synthetic duplicate, not production data.
    archive = tmp_path / "archive"
    archive.mkdir()
    for i, row in enumerate(r for r in rows if r["kind"] == "video"):
        shutil.copy2(root / row["path"], archive / f"renamed-{i}.mp4")
    (archive / "ignore.txt").write_text("not video")
    before = {p: digest_file(p) for p in root.rglob("*") if p.is_file()}
    report = verify_archive(root, rows, archive)
    assert report["all_verified"] and len(report["files"]) == 8 and not report["deletion_performed"]
    assert {p: digest_file(p) for p in before} == before
    output = tmp_path / "labels.json"
    save_label_file(output, build_label_file(work, motion, root, rows, {"video_archive": report}))
    loaded = load_history(output, archive)
    assert len(loaded.timeline.cameras) == 8
    assert not (archive / META_DIR).exists()


def test_archive_missing_copy_unindexed_file_and_same_name_wrong_content(views, tmp_path):
    root, _, _, rows = views
    archive = tmp_path / "archive"
    archive.mkdir()
    for row in (r for r in rows if r["kind"] == "video"):
        dest = archive / row["path"]
        dest.parent.mkdir(parents=True)
        shutil.copy2(root / row["path"], dest)
    bad = archive / "camera0/001.mp4"
    bad.write_bytes(b"X" * bad.stat().st_size)
    report = verify_archive(root, rows, archive)
    assert not report["all_verified"] and report["unindexed_videos"] == ["001.mp4"]
    assert report["files"][0]["status"] == "failed"
    with pytest.raises(ValueError):
        verify_archive(root, rows, root)
    with pytest.raises(InterruptedError):
        verify_archive(root, rows, archive, cancelled=lambda: True)


def test_hash_is_content_not_name(views):
    bundle, blobs = capture(views)
    for item in bundle["items"]:
        assert hashlib.sha256(blobs[item["sha256"]]).hexdigest() == item["sha256"]
        root = views[0] / META_DIR
        store_bundle(root, bundle, blobs)
        assert read_image(root, item) == blobs[item["sha256"]]


def wait_gui(app, ready, timeout=8):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while not ready() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)
    assert ready()


def test_capture_dialog_save_then_offline_gallery_and_no_qt_exceptions(views, tmp_path, monkeypatch):
    import sys

    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace import evidence_ui
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.history_window import HistoryWindow
    from cowmata_tailring.workspace.modern_window import MainWindow

    root, motion, work, rows = views
    app = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(evidence_ui, "capture_frames", lambda *a, **kw: capture_frames(*a, **kw, extractor=fake_frame))
    owner = MainWindow()
    owner.catalog = Catalog(root)
    owner.work, owner.motion, owner.rows = work, motion, rows
    owner.source_available = True
    monkeypatch.setattr(owner, "checked_cameras", lambda: list(map(str, range(8))))
    for timer in (owner.save_timer, owner.source_timer, owner.board.timer):
        timer.stop()
    dialog = evidence_ui.CaptureDialog(owner, work.project.events[0])
    dialog.show()
    wait_gui(app, lambda: dialog.bundle is not None)
    assert not dialog.save.isEnabled()
    dialog.checked.setChecked(True)
    assert dialog.save.isEnabled()
    dialog.begin_save()
    wait_gui(app, lambda: not dialog.isVisible())
    saved = work.project.events[0].extras["screenshots"]
    assert saved["human_checked"] and len(saved["items"]) == 8
    output = tmp_path / "history" / "label.json"
    doc = build_label_file(work, motion, root, rows, {})
    doc["source"]["project_root_hint"] = ""
    save_label_file(output, doc, evidence_root=root / META_DIR)
    history = HistoryWindow(output)
    history.show()
    wait_gui(app, lambda: history.data is not None)
    assert history.media_mode.currentIndex() == 1 and not history.play_button.isEnabled()
    history.review_event(work.project.events[0].id)
    from PySide6.QtWidgets import QPushButton
    assert len(history.evidence_gallery.findChildren(QPushButton)) == 8
    assert history.data.motion.sample_count == motion.sample_count
    history.close()
    owner.close()
    owner.catalog.close()
    assert not errors


def test_archive_can_support_old_confirmed_export_but_never_new_confirmation(views, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.window import MainWindow

    app = QApplication.instance() or QApplication([])
    root, motion, work, rows = views
    window = MainWindow()
    window.catalog = Catalog(root)
    window.work, window.motion, window.rows = work, motion, rows
    row = next(r for r in rows if r["kind"] == "video")
    item = {"camera": "0", "asset_id": row["asset_id"], "frame_ready": True, "verified_interval": True,
            "video_revision": window.video_revision(row), "camera_mapping_revision": "uncalibrated"}
    window.settings["video_archive"] = {"files": [{"asset_id": row["asset_id"], "status": "verified"}]}
    (root / row["path"]).unlink()
    assert not window.validate_evidence([item])
    assert window.validate_evidence([item], allow_archived=True)
    (root / row["path"]).write_bytes(b"a different recording now occupies the old path")
    assert not window.validate_evidence([item], allow_archived=True)
    window.close()
    window.catalog.close()
    app.processEvents()
