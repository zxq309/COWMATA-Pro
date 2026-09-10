"""Capture current Qt widgets after real operations in an isolated demo dataset.

No old UI screenshots or synthetic UI images are used. Farm video excerpts are
read from user-authorized originals; demo IMU and labels are not research truth.
Run with the app's private Python, supplying --source, --components and --work.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--farm", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--extras-only", action="store_true")
    parser.add_argument("--history-only", action="store_true")
    parser.add_argument("--refresh-results-only", action="store_true")
    parser.add_argument("--skip-playback", action="store_true")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    shots = args.screenshots or args.source / "assets/screenshots/manual-330"
    shots.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.source))
    from cowmata_tailring import __version__
    os.environ["VLC_HOME"] = str(args.components / "vendor/vlc")
    os.environ["VLC_PLUGIN_PATH"] = str(args.components / "vendor/vlc/plugins")
    os.environ["FFMPEG_HOME"] = str(args.components / "vendor/ffmpeg/bin")
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["OPENBLAS_NUM_THREADS"] = "2"
    from PySide6.QtCore import QSettings, Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from cowmata_tailring.app.update_ui import StartupUpdateDialog, UpdateController
    from cowmata_tailring.media.timeline import probe_media_timeline
    from cowmata_tailring.ui.about import create_about
    from cowmata_tailring.workspace import (
        algorithm_panel,
        candidate_window,
        event_models,
        organization_ui,
        rapid_backend,
    )
    from cowmata_tailring.workspace.algorithm_catalog import BEHAVIORS, HEALTH
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.clocks import wall_ms
    from cowmata_tailring.workspace.dialogs import MappingDialog, SourceTimeDialog
    from cowmata_tailring.workspace.evidence_ui import ArchiveDialog, CaptureDialog
    from cowmata_tailring.workspace.history_window import HistoryWindow
    from cowmata_tailring.workspace.modern_window import MainWindow
    from cowmata_tailring.workspace.probe import SourceInspector
    from cowmata_tailring.workspace.storage import atomic_json
    rapid_backend.MODEL_ROOT = args.components / "assets/ocr/ppocrv6_medium"
    rapid_backend.RapidV6Adapter.__init__.__defaults__ = (rapid_backend.MODEL_ROOT,)
    original_packs = event_models.available_packs
    algorithm_panel.available_packs = lambda: original_packs(args.components)
    candidate_window.available_packs = lambda: original_packs(args.components)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setOrganizationName("COWMATA-manual-validation")
    app.setApplicationName("manual-330")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(args.work / "settings"))
    organization_ui.task_root = lambda: args.work / "tasks"
    evidence = {"version": __version__, "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "dataset": "Isolated demo: generated IMU + authorized farm excerpts; labels are illustrative only",
                "operations": [], "screenshots": []}
    def wait_for(predicate=lambda: False, timeout=1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(.025)
        return bool(predicate())
    def shot(widget, name, note):
        widget.show()
        wait_for(timeout=.45)
        path = shots / (name + ".png")
        assert widget.grab().save(str(path)), name
        evidence["screenshots"].append({"file": path.name, "widget": type(widget).__name__, "operation": note})
        print("SCREENSHOT", name, flush=True)
    def record(name, value=True):
        evidence["operations"].append({"operation": name, "result": value})
        (args.work / f"capture-evidence-{time.time_ns()}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("VERIFIED", name, value, flush=True)
    def modal(call, name, note, accept=False, configure=None):
        result = []
        def capture():
            dialog = app.activeModalWidget()
            if not dialog:
                QTimer.singleShot(50, capture)
                return
            if configure:
                configure(dialog)
            shot(dialog, name, note)
            result.append(dialog.windowTitle())
            dialog.accept() if accept else dialog.reject()
        QTimer.singleShot(250, capture)
        call()
        assert result, name

    if args.history_only:
        history = HistoryWindow(args.work / "导出成果/演示.标注.json", args.work / "演示数据工程")
        history.resize(1450, 950)
        history.show()
        assert wait_for(lambda: history.data is not None, 30)
        history.media_mode.setCurrentIndex(1)
        if history.events.count():
            history.events.setCurrentRow(0)
        wait_for(timeout=.7)
        shot(history, "21-history", "Actual exported history after unified timestamp fix, with persisted evidence")
        history.close()
        record("history after timestamp fix")
        return 0
    if args.extras_only:
        window = MainWindow()
        window.confirm_close = lambda: "save"
        window.show()
        window.open_project(args.work / "演示数据工程")
        assert wait_for(lambda: window.motion is not None, 30)
        modal(lambda: MainWindow.confirm_close(window), "19-close", "Actual save/discard/cancel close prompt; cancelled to preserve work")
        menu = window.menuBar().actions()[0].menu()
        legacy = next(action.menu() for action in menu.actions() if action.menu() and action.text() == "旧版兼容")
        legacy.popup(window.mapToGlobal(window.rect().center()))
        shot(legacy, "30-legacy", "Actual legacy import/window compatibility menu")
        legacy.hide()
        tools_menu = next(action.menu() for action in window.menuBar().actions() if action.text().startswith("工具"))
        index = next(action.menu() for action in tools_menu.actions() if action.menu() and action.text() == "录像索引")
        index.popup(window.mapToGlobal(window.rect().center()))
        shot(index, "30-index-tools", "Actual pause/expand/bulk scan, recheck, copy-batch and archive controls")
        index.hide()
        window.close()
        assert wait_for(lambda: window._closed, 15)
        record("supplementary real menus and close prompt")
        return 0
    if args.refresh_results_only:
        window = MainWindow()
        window.confirm_close = lambda: "save"
        window.resize(1500, 1000)
        window.set_glass(False)
        window.show()
        project = args.work / "演示数据工程"
        window.open_project(project)
        assert wait_for(lambda: window.motion is not None and window.work is not None, 30)
        if not window.work.project.events:
            item = next(window.records.item(i) for i in range(window.records.count())
                        if window.records.item(i).text().endswith("2026-08-03_11_44_00"))
            window.select_record(item)
            assert wait_for(lambda: bool(window.work.project.events), 20)
        window.set_presentation("A")
        window.source_panel.hide()
        window.source_toggle.setChecked(False)
        if not window.event_panel.isVisible():
            window.toggle_events()
        window.body.setSizes([0, 860, 640, 0])
        for col, width in enumerate((85, 65, 178, 178, 115, 140)):
            window.events.setColumnWidth(col, width)
        window.board.play(False)
        label_index = next(i for i, label in enumerate(window.work.project.labels) if label.code == "TAIL_RAISED")
        window.labels.setCurrentIndex(label_index)
        for second in (18, 20):
            window.board.seek(wall_ms(f"2026-08-03 11:44:{second}"))
            assert wait_for(lambda: bool(window.board.tiles) and all(
                tile.ready and not tile.pending for tile in window.board.tiles.values()), 25)
            window.mark(label_index)
        assert any(d.get("confirmation") != "confirmed" for d in window.work.drafts)
        shot(window, "16-drafts", "Actual action buttons create another demo draft with current timestamp column headers")
        window.events.selectRow(0)
        assert window.selected_entry()[0] == "draft"
        old_question = QMessageBox.question
        try:
            QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
            window.delete_selected()
        finally:
            QMessageBox.question = old_question
        assert len(window.work.project.events) == 1
        assert not any(d.get("confirmation") != "confirmed" for d in window.work.drafts)
        record("additional draft created and removed through UI; confirmed event retained")
        window.board.seek(wall_ms("2026-08-03 11:44:15"))
        assert wait_for(lambda: bool(window.board.tiles) and all(
            tile.ready and not tile.pending for tile in window.board.tiles.values()), 25)
        old_directory = QFileDialog.getExistingDirectory
        try:
            QFileDialog.getExistingDirectory = lambda *a, **k: str(args.work / "导出成果")
            window.export_training()
        finally:
            QFileDialog.getExistingDirectory = old_directory
        shot(window, "20-export-result", "Actual confirmed event timestamps and renewed training export after display fix")
        assert "2026-08-03 11:44:" in window.events.item(0, 2).text()
        record("confirmed list uses full date-time", window.events.item(0, 2).text())
        history = HistoryWindow(args.work / "导出成果/演示.标注.json", project)
        history.resize(1450, 950)
        history.show()
        assert wait_for(lambda: history.data is not None, 30)
        history.media_mode.setCurrentIndex(1)
        if history.events.count():
            history.events.setCurrentRow(0)
        wait_for(timeout=.7)
        shot(history, "21-history", "Actual history list and waveform use complete date-time after display fix")
        assert "2026-08-03 11:44:" in history.events.item(0).text()
        record("history list uses full date-time", history.events.item(0).text())
        history.close()
        window.close()
        assert wait_for(lambda: window._closed, 15)
        return 0
    source = args.work / "待整理"
    project = args.work / "演示数据工程"
    base = wall_ms("2026-08-03 11:44:00")
    imu_dir = source / "九轴/546C50CA07D5-00123-w1/多层目录/原始导出"
    imu_dir.mkdir(parents=True, exist_ok=True)
    for number in range(3):
        payload = b"".join(struct.pack("<I9h", t, *[
            int(300 * math.sin(t / (520 + k * 140)) + 650 * math.exp(-((t - 12000 - k * 120) / 1500) ** 2))
            for k in range(9)]) for t in range(0, 29000, 20))
        atomic_json(imu_dir / f"2026-08-03_11_44_{number:02}.json",
                    {"version": 2, "device": "546C50CA07D5", "create_time": base - 480 * 60000,
                     "imu": base64.b64encode(payload).decode(), "demo_record": number})
    (source / "九轴/Thumbs.db").write_bytes(b"demo Windows cache")
    video_inputs = []
    for n, directory in enumerate(("视角03_神眸", "视角02_神眸"), 1):
        original = next((args.farm / directory).rglob("mb00000.mp4"))
        target = source / f"录像{n}/导出/嵌套/演示录像{n}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and not project.exists():
            # Only 30 seconds at 720p; no original is altered or copied whole.
            command = [str(args.components / "vendor/ffmpeg/bin/ffmpeg.exe"),
                       "-hide_banner", "-loglevel", "error", "-ss", str(757 if n == 1 else 16), "-i", str(original),
                       "-t", "30", "-vf", "scale=1280:-2", "-an", "-c:v", "libx264",
                       "-preset", "veryfast", "-threads", "2", "-crf", "25", str(target)]
            subprocess.run(command, check=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        video_inputs.append(source / f"录像{n}")
    window = MainWindow()
    window.confirm_close = lambda: "save"
    window.resize(1500, 1000)
    window.set_glass(False)
    window.updater = UpdateController(window, automatic=False)
    window.show()
    org = organization_ui.OrganizationWindow(window)
    org.resize(1420, 940)
    org.target.setText(str(project))
    org.add_source("imu", source / "九轴")
    for n, folder in enumerate(video_inputs, 1):
        org.add_source("video", folder, f"视角{n:02}")
    org.tabs.setCurrentIndex(1)
    org.start_date.setText("2026-08-03")
    org.category.setCurrentIndex(org.category.findData("pregnancy_late"))
    org.note.setText("教程演示；合成九轴与截取录像的对应仅供操作示范，不作为行为真值。")
    shot(org, "03-sources", "Actual source selectors; folder naming and date/category controls")
    # Capture the real category popup independently: it is a genuine Qt popup.
    org.category.showPopup()
    wait_for(timeout=.2)
    popup = org.category.view().window()
    shot(popup, "04-seven-categories", "Actual seven-category popup")
    org.category.hidePopup()
    if not project.exists():
        org.tabs.setCurrentIndex(0)
        org.start_audit()
        assert wait_for(lambda: not org.running, 40), "audit timed out"
        assert org.report
        shot(org, "05-audit", "Read-only audit completed against synthetic input files")
        record("read-only audit", len(org.report["rows"]))
        org.tabs.setCurrentIndex(2)
        shot(org, "06-report", "Actual audit result and report/resume controls")
        org.tabs.setCurrentIndex(1)
        org.preview_import()
        assert wait_for(lambda: not org.running, 40), "preview timed out"
        assert org.plan and org.execute_button.isEnabled(), org.status.text()
        shot(org, "07-preview", "Actual validated import preview, no files moved yet")
        old_question = QMessageBox.question
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        try:
            org.execute_plan()
            assert wait_for(lambda: not org.running, 40), "execute timed out"
        finally:
            QMessageBox.question = old_question
        assert org.open_button.isEnabled(), org.status.text()
        shot(org, "08-organized", "QProcess performed actual same-volume moves and indexing")
        record("organize executed", org.status.text())
    org.hide()
    # Add a separate malformed source without moving/renaming it.
    bad = args.work / "命名待核对/546C50CA07E8-y1-22207/motion"
    bad.mkdir(parents=True, exist_ok=True)
    payload = struct.pack("<I9h", 0, *([1]*9)) + struct.pack("<I9h", 20, *([1]*9))
    atomic_json(bad / "原始记录.json", {"version": 2, "device": "546C50CA07E8",
                "create_time": base-480*60000, "imu": base64.b64encode(payload).decode()})
    warning = organization_ui.OrganizationWindow(window)
    warning.resize(1450, 820)
    warning.target.setText(str(args.work / "待规范目标"))
    warning.add_source("imu", bad.parent)
    warning.start_date.setText("2026-08-03")
    warning.category.setCurrentIndex(1)
    warning.tabs.setCurrentIndex(1)
    warning.preview_import()
    assert wait_for(lambda: not warning.running, 40)
    assert warning.plan and not warning.execute_button.isEnabled()
    shot(warning, "04-naming-errors", "Swapped cow/field directory blocked; original preserved and advice visible")
    record("malformed naming blocked, source unchanged", (bad / "原始记录.json").exists())
    warning.hide()

    # Probe actual demo video timebases. The reference origin is explicitly a
    # tutorial-only manual alignment; it is never represented as recovered truth.
    cat = Catalog(project, stability_seconds=0)
    cat.scan(fast=True)
    inspector = SourceInspector(cat.root, cat.meta)
    for row in cat.pending(eager=True):
        path = cat.source_path(row["path"])
        if row["kind"] == "imu":
            cat.index_one(row["path"], inspector, eager=True)
        else:
            timeline = probe_media_timeline(path, args.components / "vendor/ffmpeg/bin/ffprobe.exe")
            camera = next(p for p in Path(row["path"]).parts if p.startswith("视角"))
            # Human readings of the two captured excerpts' first frames are
            # 11:43:59 / 11:43:58; source seek offsets are not clock truth.
            video_start = base - (1000 if camera == "视角01" else 2000)
            metadata = {"camera": camera, "duration_ms": 30000, "timeline": timeline.to_dict(),
                        "demo_alignment": True, "start_display": "2026-08-03 11:43:" + ("59" if camera == "视角01" else "58"),
                        "manual_readings": [{"media_ms": 0, "wall_ms": video_start},
                                            {"media_ms": 29000, "wall_ms": video_start+29000}],
                        "intervals": [{"wall_start": video_start, "wall_end": video_start+30000,
                                       "media_start": 0, "media_end": 30000, "verified": True,
                                       "warnings": ["教程人工演示对应，不作采集时间或行为真值"]}]}
            cat.index_one(row["path"], lambda *_: metadata, eager=True)
    cat.close()
    window.open_project(project)
    assert wait_for(lambda: window.motion is not None and len(window.board.tiles) > 0, 45), "workspace not loaded"
    window.source_panel.show()
    window.source_toggle.setChecked(True)
    window.body.setSizes([300, 1100, 0, 0])
    def ready_at(offset):
        window.board.play(False)
        window.board.seek(base + offset)
        return wait_for(lambda: any(t.ready and not t.pending for t in window.board.tiles.values()), 20)
    assert ready_at(4000)
    window.plot.set_view(0, window.motion.duration_ms)
    shot(window, "09-workspace", "Actual organized project opened with decoded original video excerpt")
    record("opened organized project", {"records": window.records.count(), "cameras": window.cameras.count(),
                                       "cow": window.cow.text(), "category": window.data_category.currentData()})
    modal(window.source_manager, "10-index", "Actual indexed IMU/video table with translated index states")
    window.show_status_details()
    shot(window.status_dialog, "10-status", "Actual full loading status log")
    window.status_dialog.hide()
    row = next(r for r in window.rows if r["kind"] == "video")
    time_dialog = SourceTimeDialog(window.catalog, row, window)
    time_dialog.show()
    time_dialog.load_frame()
    assert wait_for(lambda: not time_dialog.canvas.image.isNull(), 15)
    shot(time_dialog, "11-time-roi", "Actual video source timestamp/ROI dialog loading a decoded frame")
    time_dialog.reject()
    # Show and exercise all view modes against actual decoder output.
    window.board.select(window.checked_cameras()[:1])
    window.board.enlarge(window.board.main_camera)
    assert ready_at(6000)
    window.set_presentation("A")
    shot(window, "12-single-view", "Actual single view with its progress bar and hover transport")
    if not args.skip_playback:
        for speed in (1., 2., 4.):
            window.board.set_rate(speed)
            window.board.play(True)
            wait_for(timeout=.8)
            window.board.play(False)
        record("single-view playback speeds exercised", [1, 2, 4])
    window.board.set_rate(1.)
    window.board.enlarge(None)
    for i in range(window.cameras.count()):
        window.cameras.item(i).setCheckState(Qt.CheckState.Checked)
    window.select_cameras()
    window.set_presentation("B")
    assert ready_at(8000)
    wait_for(lambda: all(t.ready and not t.pending for t in window.board.tiles.values()), 20)
    shot(window, "13-multiview", "Actual two-view grid and shared reference timestamps")
    window.set_presentation("C")
    shot(window, "14-waveform", "Actual waveform layout with movable picture-in-picture")
    window.set_presentation("A")
    # UI action records manual correspondence. Synthetic demo only.
    window.seek_imu(8000)
    assert ready_at(8000)
    window.pin()
    window.seek_imu(23000)
    assert ready_at(23000)
    window.pin()
    mapping = MappingDialog(window.work.clock, window)
    shot(mapping, "15-alignment", "Actual two manually pinned demo correspondences")
    mapping.reject()
    record("manual sync anchors", len(window.work.clock.anchors))
    # Real marking buttons create the demo interval, then real confirmation
    # extracts evidence through FFmpeg in CaptureDialog.
    label_index = next(i for i, label in enumerate(window.work.project.labels) if label.code == "TAIL_RAISED")
    window.labels.setCurrentIndex(label_index)
    assert ready_at(10000)
    window.mark(label_index)
    shot(window, "16-active-action", "Actual active action, start timestamp and explicit end/cancel controls")
    assert ready_at(15000)
    window.mark(label_index)
    assert window.work.drafts
    window.toggle_events()
    window.body.setSizes([260, 860, 350, 0])
    shot(window, "16-drafts", "Actual action-start/action-end buttons produced a saved video draft")
    window.events.selectRow(0)
    window.refine_selected()
    shot(window, "16-imu-refine", "Selected video draft projected onto the IMU for independent boundary adjustment")
    modal(window.edit_selected, "16-boundary-editor", "Actual independent start/end timestamp controls; cancel retains boundaries")
    # Confirmation calls modal evidence capture when visible. Capture and save
    # through its controls inside a timed callback.
    def evidence_action():
        dialog = app.activeModalWidget()
        if not isinstance(dialog, CaptureDialog) or dialog.future is not None or dialog.bundle is None:
            QTimer.singleShot(100, evidence_action)
            return
        dialog.checked.setChecked(True)
        shot(dialog, "17-evidence", "Actual FFmpeg extraction from both demo videos, reviewed and saved")
        assert dialog.save.isEnabled(), dialog.status.text()
        dialog.begin_save()
    QTimer.singleShot(150, evidence_action)
    window.confirm_selected()
    assert window.work.project.events
    event = window.work.project.events[0]
    assert wait_for(lambda: bool(event.extras.get("screenshots")), 45), "evidence save timed out"
    assert event.extras.get("screenshots"), "evidence not saved"
    record("draft confirmed with actual evidence files", len(event.extras["screenshots"]["items"]))
    window.events.selectRow(0)
    modal(window.edit_selected, "18-edit-label", "Actual edit dialog; cancelling preserves confirmed boundaries")
    modal(window.finish_record, "19-finish", "Actual per-record save/completion choices; no automatic completion")
    # Exercise a save, switch and resume.
    window.save_current()
    first_id = window.work.asset_id
    other_item = next(window.records.item(i) for i in range(window.records.count())
                      if window.records.item(i).data(Qt.ItemDataRole.UserRole)["asset_id"] != first_id)
    window.select_record(other_item)
    assert wait_for(lambda: window.work and window.work.asset_id != first_id, 10)
    assert wait_for(lambda: not window._loading_path and not window._reading_path, 10)
    first_item = next(window.records.item(i) for i in range(window.records.count())
                      if window.records.item(i).data(Qt.ItemDataRole.UserRole)["asset_id"] == first_id)
    window.select_record(first_item)
    assert wait_for(lambda: window.work and window.work.asset_id == first_id, 10)
    record("switch/auto-save/resume", bool(window.work.project.events))
    window.reopen_record()
    window.save_current()
    # Export real full/snippet/training files via current window handlers.
    exported = args.work / "导出成果"
    exported.mkdir(exist_ok=True)
    old_save, old_directory = QFileDialog.getSaveFileName, QFileDialog.getExistingDirectory
    try:
        full = exported / "演示.标注.json"
        QFileDialog.getSaveFileName = lambda *a, **k: (str(full), "JSON")
        window.export_work()
        assert wait_for(lambda: not window._export_running, 20) and full.is_file()
        window.selection = [9000, 17000]
        part = exported / "演示.片段.标注.json"
        QFileDialog.getSaveFileName = lambda *a, **k: (str(part), "JSON")
        window.export_work(snippet=True)
        assert wait_for(lambda: not window._export_running, 20) and part.is_file()
        QFileDialog.getExistingDirectory = lambda *a, **k: str(exported)
        window.export_training()
    finally:
        QFileDialog.getSaveFileName, QFileDialog.getExistingDirectory = old_save, old_directory
    record("full / snippet / training export files", [p.name for p in exported.rglob("*") if p.is_file()])
    shot(window, "20-export-result", "Actual training export completion message and retained labels")
    exports_menu = next(a.menu() for a in window.menuBar().actions()[0].menu().actions() if a.menu() and a.text()=="导出")
    exports_menu.popup(window.mapToGlobal(window.rect().center()))
    shot(exports_menu, "20-export", "Actual full/snippet/training export menu; output files verified")
    exports_menu.hide()
    history = HistoryWindow(full, project)
    history.resize(1450, 950)
    history.show()
    assert wait_for(lambda: history.data is not None, 30)
    history.media_mode.setCurrentIndex(1)
    if history.events.count():
        history.events.setCurrentRow(0)
    wait_for(timeout=.7)
    shot(history, "21-history", "Actual exported file reopened read-only with saved evidence images")
    record("history reopens exported results", history.events.count())
    history.close()
    modal(window.team_settings, "22-team", "Actual team inbox settings; conflict policy visible")
    window.event_panel.hide()
    window.body.setSizes([260, 1100, 0, 0])
    window.open_algorithm(BEHAVIORS[2])
    if window.algorithm_panel.bindings:
        window.algorithm_panel.start()
        assert wait_for(lambda: not window.algorithm_panel.running, 90)
    shot(window, "23-algorithm", "Actual registered behavior model run; results remain separate from labels")
    record("algorithm inspection", window.algorithm_panel.status.text())
    window.exit_algorithm()
    candidate = candidate_window.CandidateWindow(window)
    candidate.show()
    if candidate.models.count():
        candidate.start()
        assert wait_for(lambda: not candidate.running, 90)
    shot(candidate, "24-candidates", "Actual candidate scan against demo IMU; no inference treated as truth")
    record("candidate scan", candidate.status.text())
    candidate.close()
    window.open_algorithm(HEALTH[2])
    assert not window.algorithm_panel.run_button.isEnabled()
    shot(window, "25-health-boundary", "Actual pregnancy algorithm entry is disabled/unconnected")
    window.exit_algorithm()
    window.options.show()
    shot(window.options, "26-settings", "Actual view, playback, strict review and compatibility settings")
    window.options.hide()
    modal(window.diagnostics, "26-diagnostics", "Actual decoder timing and cache diagnostics after playback")
    archive = ArchiveDialog(window)
    shot(archive, "27-archive", "Actual archive verification UI; originals are not removed")
    archive.reject()
    about = create_about(window)
    shot(about, "28-about", "Current version/build and tutorial/update entries")
    about.close()
    window.updater.open_dialog()
    shot(window.updater.dialog, "28-updates", "Actual startup/background update policy and manual controls")
    window.updater.dialog.hide()
    modal(window.quick_help, "29-help", "Actual F1 quick-start help aligned with organize-label-export")
    gate = StartupUpdateDialog()
    shot(gate, "02-startup", "Actual startup latest-release check against configured GitHub endpoint")
    gate.updater.stop.set()
    gate.reject()
    window.save_current()
    window.close()
    wait_for(lambda: window._closed, 20)
    record("normal close after save", window._closed)
    print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
