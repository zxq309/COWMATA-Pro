"""Native screenshot/archive acceptance on isolated, explicitly marked fixtures.

Real decoders and IMU; illustrative clocks/labels are NOT scientific ground truth.
No caller-owned file is modified. An existing output directory is never reused.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import META_DIR, Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.evidence import evidence_summary
from cowmata_tailring.workspace.evidence_ui import ArchiveDialog, CaptureDialog
from cowmata_tailring.workspace.history_window import HistoryWindow
from cowmata_tailring.workspace.label_file import build_label_file, load_history, read_index, save_label_file
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    origin, out = args.project.resolve(), args.out.resolve()
    original_rows, settings = read_index(origin)
    if settings.get("fixture") is not True:
        raise ValueError("Only explicitly marked isolated test fixtures are accepted")
    out.mkdir(parents=True, exist_ok=False)
    root = out / "原片测试工程"
    root.mkdir()
    originals = {}
    for row in original_rows:
        if row["kind"] not in {"video", "imu"} or row["state"] not in {"ready", "review"}:
            continue
        source = origin / row["path"]
        if not source.resolve().is_relative_to(origin):
            raise ValueError("Fixture source escapes origin")
        sha = digest_file(source)
        assert sha == row["asset_id"]
        originals[row["path"]] = sha
        target = root / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(now=1)
    for row in original_rows:
        if row["path"] in originals:
            catalog.index_one(row["path"], lambda *args, data=row["metadata"]: copy.deepcopy(data), now=2)
    rows = catalog.rows()
    imu = next(r for r in rows if r["kind"] == "imu" and r["state"] == "ready")
    motion = load_motion_json(root / imu["path"])
    timeline = VideoTimeline(intervals_from_rows(rows))
    base = max(min(s.wall_start for s in timeline.intervals if s.camera == c) for c in timeline.cameras)
    work = SessionWork(imu["asset_id"])
    work.project.cow_id = "DEMO-ONLY · 非科研真值"
    work.clock = ClockMap([Anchor(0, base), Anchor(30000, base + 30000)])
    event = work.project.add_event(0, 2000, 10000, note="UI ACCEPTANCE ONLY, NOT COW TRUTH")
    event.extras.update(confirmation="confirmed", mapping_revision=work.clock.revision)
    settings = {"fixture": True, "selected_cameras": timeline.cameras[:8], "current_asset": work.asset_id}
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    errors = []
    sys.excepthook = lambda *values: errors.append("".join(traceback.format_exception(*values)))
    owner = MainWindow()
    owner.catalog, owner.rows, owner.settings = catalog, rows, settings
    owner.motion, owner.work, owner.source_available = motion, work, True
    owner.checked_cameras = lambda: timeline.cameras[:8]
    for timer in (owner.save_timer, owner.source_timer, owner.board.timer):
        timer.stop()
    # Disable the normal playback board for the capture timing measurement; the
    # capture worker always decodes original sources with two bounded workers.
    owner.board.configure(catalog, rows, timeline)
    owner.refresh_events()
    owner.show()
    last = time.perf_counter()
    beats = []
    def beat():
        nonlocal last
        now = time.perf_counter()
        beats.append((now - last) * 1000)
        last = now
    heartbeat = QTimer()
    heartbeat.setInterval(25)
    heartbeat.timeout.connect(beat)
    heartbeat.start()
    def wait_for(condition, timeout=100):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
            if errors:
                raise AssertionError(errors)
        assert condition(), "Native GUI timed out"
    dialog = CaptureDialog(owner, event)
    dialog.position.setValue(5.0)
    dialog.show()
    begin = time.perf_counter()
    wait_for(lambda: dialog.bundle is not None)
    capture_seconds = time.perf_counter() - begin
    capture_beats = list(beats)
    assert all(i["status"] == "captured" for i in dialog.bundle["items"]), dialog.bundle
    assert len(dialog.bundle["items"]) == len(timeline.cameras[:8])
    QTest.qWait(300)
    dialog.grab().save(str(out / "capture-native.png"))
    dialog.checked.setChecked(True)
    dialog.begin_save()
    wait_for(lambda: not dialog.isVisible())
    bundle = event.extras["screenshots"]
    assert bundle["human_checked"] and not bundle["algorithm_input"]
    archive = out / "外部录像归档"
    archive.mkdir()
    for index, row in enumerate(r for r in rows if r["kind"] == "video"):
        shutil.copy2(root / row["path"], archive / f"归档录像-{index:03d}.mp4")
    archive_dialog = ArchiveDialog(owner)
    choose = QFileDialog.getExistingDirectory
    QFileDialog.getExistingDirectory = lambda *a, **kw: str(archive)
    try:
        archive_dialog.show()
        archive_dialog.begin()
    finally:
        QFileDialog.getExistingDirectory = choose
    wait_for(lambda: archive_dialog.report is not None)
    report = archive_dialog.report
    assert report["all_verified"] and not report["deletion_performed"]
    archive_dialog.grab().save(str(out / "archive-check-native.png"))
    archive_dialog.spot.setChecked(True)
    archive_dialog.evidence.setChecked(True)
    archive_dialog.save_report()
    archive_dialog.close()
    settings["video_archive"] = report
    document = build_label_file(work, motion, root, rows, settings, selection=(1000, 12000))
    document["source"]["project_root_hint"] = ""
    output = out / "可移动成果" / "九轴片段.标注.json"
    save_label_file(output, document, evidence_root=catalog.meta)
    photo_bytes = sum(i["bytes"] for i in bundle["items"])
    offline = load_history(output)
    assert not offline.timeline.intervals and offline.motion is not None
    assert evidence_summary(document, output.parent)["saved"] == len(bundle["items"])
    history = HistoryWindow(output)
    history.show()
    wait_for(lambda: history.data is not None)
    assert history.media_mode.currentIndex() == 1
    QTest.qWait(300)
    history.grab().save(str(out / "offline-history-native.png"))
    # Actual full-resolution evidence viewing remains available offline.
    QTimer.singleShot(250, lambda: [w.accept() for w in app.topLevelWidgets()
                                  if hasattr(w, "accept") and "原始分辨率" in w.windowTitle()])
    buttons = history.evidence_gallery.findChildren(QPushButton)
    assert len(buttons) == len(bundle["items"])
    buttons[0].click()
    assert len(load_history(output, archive).timeline.cameras) == len(timeline.cameras)
    history.close()
    heartbeat.stop()
    owner.close()
    catalog.close()
    assert not errors
    assert all(digest_file(origin / path) == sha for path, sha in originals.items())
    assert all(digest_file(root / path) == sha for path, sha in originals.items())
    values = sorted(beats)
    result = {"passed": True, "fixture_only": True, "scientific_labels": False,
              "views": len(bundle["items"]), "capture_seconds": capture_seconds,
              "evidence_image_bytes": photo_bytes, "max_frame_delta_ms": max(abs(i["delta_ms"]) for i in bundle["items"]),
              "gui_heartbeat_p95_ms": values[int(.95 * (len(values) - 1))],
              "gui_heartbeat_max_ms": max(values), "archive_files_verified": len(report["files"]),
              "capture_heartbeat_max_ms": max(capture_beats),
              "renamed_archive_relinked_without_index": True, "offline_image_zoom": True,
              "originals_unchanged": True, "qt_exceptions": errors, "annotation": str(output)}
    atomic_json(out / "report.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
