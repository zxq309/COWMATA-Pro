"""Native VLC + asynchronous IMU handoff acceptance, using isolated test-only data.

Three 8-second synthetic records: first two calibrated, third uncalibrated.
Copies two existing synthetic camera fixtures; never edits original user data.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from cowmata_tailring.annotation.data import _synthetic_object
from cowmata_tailring.workspace.catalog import META_DIR, Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, wall_ms
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.window import MainWindow
from cowmata_tailring.workspace.work import SessionWork


def make_project(source, output, view_count=2):
    output.mkdir(parents=True, exist_ok=False)
    base = wall_ms("2026-08-03 12:00:00")
    with sqlite3.connect((source / META_DIR / "index.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        metadata = {asset: json.loads(raw) for asset, raw in db.execute("SELECT id,metadata FROM assets WHERE kind='video'")}
    for view in range(1, view_count + 1):
        for path in sorted((source / f"视角{view}").rglob("*.mp4")):
            target = output / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for part in range(3):
        obj = _synthetic_object(2)
        obj.update(uid=part + 1, device="TEST-HANDOFF", create_time=base + 3600000 + part * 8000)
        # Deliberately unrelated receive time: it must NOT drive continuation.
        frames = b"".join(struct.pack("<I9h", 1000 + ms, 4096, part * 300, -1024,
                                      ms % 1000, 40, 60, 100, -100, 500)
                          for ms in range(0, 8001, 20))
        obj["imu"] = base64.b64encode(frames).decode("ascii")
        atomic_json(output / "九轴数据" / f"{part + 1:03d}.json", obj)
    catalog = Catalog(output, stability_seconds=0)
    assets = {}
    try:
        catalog.scan()
        inspector = SourceInspector(output, catalog.meta)
        for row in catalog.pending(retry_seconds=0):
            result = catalog.index_one(row["path"], lambda path, kind, asset:
                                       inspector(path, kind, asset) if kind == "imu" else copy.deepcopy(metadata[asset]))
            if row["kind"] != "imu":
                continue
            part = int(Path(row["path"]).stem) - 1
            asset = result["asset_id"]
            assets[part] = asset
            work = SessionWork(asset)
            work.project.cow_id = "TEST-ONLY-COW"
            if part < 2:
                start = base + 4000 + part * 8000
                work.clock = ClockMap([Anchor(0, start), Anchor(8000, start + 8000)])
            work.progress = {"reference_ms": base + 4000, "imu_ms": 0}
            atomic_json(catalog.work_path(asset), work.to_dict())
        catalog.save_settings({"fixture": True, "current_asset": assets[0], "device": "TEST-HANDOFF",
                               "reference_ms": base + 4000, "selected_cameras": [f"视角{i + 1}" for i in range(view_count)]})
    finally:
        catalog.close()
    hashes = {str(p.relative_to(output)): digest_file(p) for p in output.rglob("*")
              if p.is_file() and META_DIR not in p.parts}
    return base, assets, hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-fixture", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--views", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--ui", choices=("classic", "modern"), default="classic")
    parser.add_argument("--exercise-layouts", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve()
    base, assets, hashes = make_project(args.source_fixture.resolve(), project, args.views)
    app = QApplication([])
    # Keep test progress separate from the user's last opened project preference.
    app.setOrganizationName("COWMATA-Validation")
    app.setApplicationName("IMU-Handoff-Test")
    window_class = MainWindow
    if args.ui == "modern":
        from cowmata_tailring.workspace.modern_window import MainWindow as ModernWindow
        window_class = ModernWindow
    window = window_class()
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.setWindowTitle("COWMATA 续接实测 · 合成素材 · 自动关闭")
    window.resize(1280, 880)
    window.show()
    window.open_project(project)
    started = time.perf_counter()
    playing_at = None
    samples = []
    saw = {f"视角{i + 1}": set() for i in range(args.views)}
    result = {}
    layout_checks = []

    def finish(timed_out):
        timer.stop()
        active = [s for s in samples if s["playing"]]
        ids = {s["imu_asset"] for s in samples}
        result.update(timed_out=timed_out, rate=args.rate, views=args.views, ui=args.ui, actual_native_vlc=True, synthetic_test_data=True,
                      handoff_observed=assets[1] in ids, uncalibrated_not_loaded=assets[2] not in ids,
                      reference_never_rewound=all(b["reference_ms"] >= a["reference_ms"] for a, b in zip(active, active[1:])),
                      video_kept_playing=bool(active) and window.board.playing,
                      all_views_crossed_files=all(len(v) >= 2 for v in saw.values()),
                      all_views_ready=all(tile.ready for tile in window.board.tiles.values()),
                      uncalibrated_prompt=any("未强行拼接" in s["alignment"] for s in samples),
                      sources_unchanged=all(digest_file(project / p) == digest for p, digest in hashes.items()),
                      first_all_ready_seconds=playing_at,
                      elapsed_seconds=time.perf_counter() - started)
        if args.exercise_layouts:
            result["layout_switches_preserved_handles_clock_and_seek_generation"] = len(layout_checks) >= 6 and all(layout_checks)
        result["passed"] = all(v for k, v in result.items() if isinstance(v, bool) and k != "timed_out") and not timed_out
        window.board.play(False)
        window.close()
        atomic_json(args.out, {"summary": result, "assets": assets, "samples": samples, "layout_checks": layout_checks})
        print(json.dumps(result, ensure_ascii=True), flush=True)
        app.quit()

    def tick():
        nonlocal playing_at
        elapsed = time.perf_counter() - started
        ready = len(window.board.tiles) == args.views and all(t.ready and not t.pending for t in window.board.tiles.values())
        if playing_at is None and window.work and window.work.asset_id == assets[0] and ready:
            window.board.set_rate(args.rate)
            window.board.play(True)
            playing_at = elapsed
        if args.exercise_layouts and args.ui == "modern" and playing_at is not None and elapsed - playing_at > (len(layout_checks) + 1) * 2 and len(layout_checks) < 6:
            def state():
                return (window.board.reference_ms, window.board.generation, window.board.playing, window.board.rate,
                        [(c, id(t.engine), int(t.surface.winId()), t.asset_id) for c, t in window.board.tiles.items()])
            before = state()
            window.set_presentation("BCAABC"[len(layout_checks)])
            if len(layout_checks) == 5:
                # Actual native PiP zoom must return to a large A view without
                # replacing engines or seeking any selected camera.
                window.board.enlarge(window.board.main_camera)
                assert window.stage.mode == "A" and window.board.expanded == window.board.main_camera
            layout_checks.append(before == state())
        for camera, tile in window.board.tiles.items():
            if tile.ready:
                saw[camera].add(tile.asset_id)
        samples.append({"elapsed": elapsed, "reference_ms": window.board.reference_ms,
            "imu_ms": window.imu_ms, "imu_asset": window.work.asset_id if window.work else None, "playing": window.board.playing,
            "alignment": window.alignment_label.text(), "coverage": window.coverage_label.text(),
            "views": [{"camera": name, "asset": tile.asset_id, "ready": tile.ready,
                       "phase": tile.pending.get("phase") if tile.pending else None,
                       "displayed": tile.engine.stats().displayed_pictures if tile.engine and tile.engine.stats() else 0}
                      for name, tile in window.board.tiles.items()]})
        if window.board.reference_ms >= base + 24000 and ready:
            finish(False)
        elif elapsed > 70:
            finish(True)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    app.exec()
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
