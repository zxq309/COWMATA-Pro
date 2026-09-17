"""Build an isolated copy of indexed real samples, render actual Qt layouts.

No claimed IMU/video synchronization is introduced for screenshot purposes.
The original sample project is opened read-only and never written.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.catalog import META_DIR, Catalog, digest_file
from cowmata_tailring.workspace.clocks import wall_ms
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def make_project(source, output):
    output.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect((source / META_DIR / "index.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        metadata = {asset: json.loads(raw) for asset, raw in db.execute("SELECT id,metadata FROM assets")}
    hashes = {}
    for path in source.rglob("*"):
        if not path.is_file() or META_DIR in path.relative_to(source).parts or path.suffix.lower() not in {".json", ".mp4"}:
            continue
        target = output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        hashes[str(target.relative_to(output))] = digest_file(target)
    catalog = Catalog(output, stability_seconds=0)
    try:
        catalog.scan()
        for row in catalog.pending(retry_seconds=0):
            catalog.index_one(row["path"], lambda path, kind, asset: copy.deepcopy(metadata[asset]))
        original_settings = json.loads((source / META_DIR / "project.json").read_text(encoding="utf-8"))
        # A manual browsing position, NOT a synchronization anchor.
        work = SessionWork(original_settings["current_asset"])
        work.progress = {"imu_ms": 0, "reference_ms": wall_ms("2026-08-03 11:40:00")}
        atomic_json(catalog.work_path(work.asset_id), work.to_dict())
        catalog.save_settings({"fixture": True, "reference_ms": wall_ms("2026-08-03 11:40:00"),
                               "selected_cameras": original_settings.get("selected_cameras", []),
                               "current_asset": original_settings.get("current_asset"),
                               "device": original_settings.get("device")})
    finally:
        catalog.close()
    return hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reuse-test-project", action="store_true")
    args = parser.parse_args()
    if args.reuse_test_project:
        settings = json.loads((args.project / META_DIR / "project.json").read_text(encoding="utf-8"))
        if settings.get("fixture") is not True or args.source.resolve() == args.project.resolve():
            raise ValueError("Only an isolated validation fixture may be reused")
        hashes = {str(p.relative_to(args.project)): digest_file(p) for p in args.project.rglob("*")
                  if p.is_file() and META_DIR not in p.relative_to(args.project).parts}
    else:
        hashes = make_project(args.source.resolve(), args.project.resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setOrganizationName("COWMATA-Validation")
    app.setApplicationName("Modern-UI-Preview")
    w = MainWindow()
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    w.show()
    w.open_project(args.project)
    started = time.perf_counter()
    cases = [("A", 1600, 1000), ("B", 1600, 1000), ("C", 1600, 1000), ("A", 1280, 800), ("B", 1920, 1080)]
    result = {"real_source_samples": True, "alignment_not_fabricated": True, "screenshots": []}
    case_index = 0

    def finish(ok):
        timer.stop()
        w.grab().save(str(args.out / "last-state.png"))
        result["last_status"] = {"banner": w.banner.text(), "alignment": w.alignment_label.text(),
                                 "tiles": {c: {"ready": t.ready, "status": t.message.text()} for c, t in w.board.tiles.items()}}
        result["passed"] = ok
        result["sources_unchanged"] = all(digest_file(args.project / p) == digest for p, digest in hashes.items())
        w.close()
        atomic_json(args.out / "ui-preview.json", result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        app.quit()

    def capture():
        nonlocal case_index
        if not w.motion or not w.board.tiles or not all(t.ready and t.frame_image is not None for t in w.board.tiles.values()):
            if time.perf_counter() - started > 90:
                result["error"] = w.banner.text()
                finish(False)
            return
        if case_index >= len(cases):
            finish(True)
            return
        mode, width, height = cases[case_index]
        w.set_presentation(mode)
        w.resize(width, height)
        if case_index == 0:
            w.plot.set_view(0, 60000)
            w.tell("真实样例浏览：九轴与视频尚未校准，当前画面不表示已同步真值。")
        def save():
            path = args.out / f"layout-{mode}-{width}x{height}.png"
            w.grab().save(str(path))
            result["screenshots"].append({"path": str(path), "actual_size": [w.width(), w.height()], "mode": mode,
                "reference_ms": w.board.reference_ms, "frame_assets": {c: t.asset_id for c, t in w.board.tiles.items()}})
        QTimer.singleShot(200, save)
        case_index += 1

    timer = QTimer()
    timer.timeout.connect(capture)
    timer.start(700)
    app.exec()
    return 0 if result.get("passed") and result.get("sources_unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
