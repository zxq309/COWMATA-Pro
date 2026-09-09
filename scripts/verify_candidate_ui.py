"""Native GUI + real five-model run in a NEW isolated TEST-ONLY project.

Copies a small existing video fixture; never writes the provided real source.
Synthetic clock anchors are solely for interface testing, not cow truth.
"""
import argparse
import base64
import json
from pathlib import Path
import shutil
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.app.resources import prioritize_ui
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.label_file import load_history, read_index, read_label_file
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    raw_hash = digest_file(args.raw)
    rows, settings = read_index(args.fixture)
    assert settings.get("fixture") is True
    root = out / "TEST-ONLY-project"
    root.mkdir()
    metadata = {}
    for row in rows:
        if row["kind"] == "video":
            target = root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.fixture / row["path"], target)
            assert digest_file(target) == row["asset_id"]
            metadata[row["path"]] = row["metadata"]
            metadata[row["path"]]["timeline"]["source_path"] = str(target)
            # Fixture anchors are intentionally manual TEST ONLY times, not
            # old automatically indexed OCR that should be upgraded on opening.
            spans = metadata[row["path"]]["intervals"]
            metadata[row["path"]]["manual_readings"] = [
                {"media_ms": spans[0]["media_start"], "wall_ms": spans[0]["wall_start"]},
                {"media_ms": spans[-1]["media_end"], "wall_ms": spans[-1]["wall_end"]}]
    raw = root / "IMU" / "real-record.json"
    raw.parent.mkdir()
    shutil.copy2(args.raw, raw)
    motion = load_motion_json(raw)
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan()
    inspector = SourceInspector(root, catalog.meta)
    for row in catalog.pending(retry_seconds=0):
        catalog.index_one(row["path"], lambda p, k, a: metadata[p.relative_to(root).as_posix()] if k == "video" else inspector(p, k, a))
    work = SessionWork(raw_hash)
    work.project.cow_id = "TEST-ONLY-NOT-FIELD-TRUTH"
    base = min(x["intervals"][0]["wall_start"] for x in metadata.values())
    work.clock = ClockMap([Anchor(0, base), Anchor(motion.duration_ms, base + motion.duration_ms)])
    work.progress = {"imu_ms": 4000, "reference_ms": base + 4000}
    atomic_json(catalog.work_path(raw_hash), work.to_dict())
    catalog.save_settings({"fixture": True, "current_asset": raw_hash, "device": motion.device,
                           "reference_ms": base + 4000, "selected_cameras": [f"CAM{i + 1:02d}" for i in range(8)]})
    catalog.close()
    app = QApplication([])
    prioritize_ui()
    app.setStyle("Fusion")
    app.setOrganizationName("COWMATA-Validation")
    app.setApplicationName("Candidate-UI-Test")
    window = MainWindow()
    window.confirm_close = lambda: "save"  # This isolated test owns its work.
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.show()
    window.open_project(root)
    state, began = "load", time.monotonic()
    result = {"fixture_only": True, "synthetic_alignment_not_truth": True}
    exported = out / "one-file.annotations.json"
    beats = []
    last_beat = time.monotonic()

    def heartbeat():
        nonlocal last_beat
        now = time.monotonic()
        if state == "predict":
            beats.append((now - last_beat) * 1000)
        last_beat = now

    def finish(error=None):
        timer.stop()
        pulse.stop()
        result["error"] = error
        result["passed"] = error is None and digest_file(args.raw) == raw_hash
        result["original_unchanged"] = digest_file(args.raw) == raw_hash
        window.close()
        atomic_json(out / "report.json", result)
        print(json.dumps(result), flush=True)
        app.quit()

    def tick():
        nonlocal state
        if time.monotonic() - began > 150:
            finish("Timeout in " + state)
            return
        try:
            if state == "load" and window.motion and len(window.board.tiles) == 8 and all(t.ready for t in window.board.tiles.values()):
                window.playback_policy.setCurrentIndex(0)  # Exercise inference's full-speed protection.
                window.open_candidates()
                window._candidate_window.start()
                window.board.play(True)
                state = "predict"
            elif state == "predict" and not window._candidate_window.running:
                runs = window.work.project.extras.get("event_model_runs", {})
                assert len(runs) == 5, window._candidate_window.audit.toPlainText()
                assert not window.work.project.events and not window.work.drafts
                assert window.board.playback_policy == "balanced" and window.playback_policy.currentIndex() == 1
                result["automatic_load_protection"] = True
                result["real_model_candidates"] = {r["identity"]["model_id"]: len(r["candidates"]) for r in runs.values()}
                dialog = window._candidate_window
                dialog.items.setCurrentRow(1)
                run, candidate = dialog.selected()
                # TEST ONLY known correspondence: candidate -> fixture second 25.
                shift = base + 25000 - candidate["point_ms"]
                window.work.set_clock(ClockMap([Anchor(0, shift), Anchor(motion.duration_ms, shift + motion.duration_ms)]))
                window.update_alignment_text()
                dialog.review()
                result["candidate_count"] = dialog.items.count()
                result["prediction_ui_p95_ms"] = float(np.percentile(beats, 95))
                result["prediction_ui_max_ms"] = max(beats)
                assert result["prediction_ui_p95_ms"] < 150 and result["prediction_ui_max_ms"] < 750
                dialog.grab().save(str(out / "candidate-dialog.png"))
                dialog.close()
                state = "review"
            elif state == "review" and all(t.ready for t in window.board.tiles.values()):
                assert abs(window.board.reference_ms - (base + 25000)) < 1
                result["candidate_located_actual_video"] = True
                window.grab().save(str(out / "workspace.png"))
                QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(exported), "JSON"))
                window.export_work()
                state = "export"
            elif state == "export" and not window._export_running:
                doc = read_label_file(exported)
                assert base64.b64decode(doc["embedded_imu"]["original_json_base64"]) == args.raw.read_bytes()
                assert len(doc["work"]["project"]["event_model_runs"]) == 5
                restored = load_history(exported)
                assert restored.motion.sample_count == motion.sample_count
                result["export_contains_exact_record_and_five_runs"] = True
                result["pool_size"] = len(window.board.pool)
                assert result["pool_size"] <= 9
                finish()
        except Exception as exc:
            finish(repr(exc))

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(300)
    pulse = QTimer()
    pulse.timeout.connect(heartbeat)
    pulse.start(20)
    app.exec()
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
