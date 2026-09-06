"""Offline/moved-package smoke checks using only the embedded interpreter."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--video")
    parser.add_argument("--json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    assert Path(sys.executable).resolve().is_relative_to(root / "runtime")
    # Deliberately poison the installed-tool search and disable Python network use.
    os.environ["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ["VLC_PLUGIN_PATH"] = str(root / "vendor" / "vlc" / "plugins")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    def deny_network(*_args, **_kwargs):
        raise AssertionError("Portable self-test attempted a network connection")
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    import cv2
    import numpy
    import onnxruntime
    import pandas
    import scipy
    import sklearn
    from PySide6 import __version__ as qt_version
    from PySide6.QtWidgets import QApplication, QWidget

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.media.engine import MediaEngine
    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg, probe_media
    from cowmata_tailring.workspace.catalog import Catalog, digest_file
    from cowmata_tailring.workspace.modern_window import MainWindow
    from cowmata_tailring.workspace.ocr import TimestampOCR
    from cowmata_tailring.workspace.probe import extract_frame
    from cowmata_tailring.workspace.storage import atomic_json
    app = QApplication([])
    ffmpeg, ffprobe = find_ffmpeg()
    assert Path(ffmpeg).is_relative_to(root / "vendor")
    assert Path(ffprobe).is_relative_to(root / "vendor")
    surface = QWidget()
    engine = MediaEngine(surface)
    result = {"python": sys.version, "executable": sys.executable, "qt": qt_version, "vlc": engine.vlc_version,
              "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe), "PATH": os.environ["PATH"],
              "python_network_blocked": True, "qt_platform": "offscreen",
              "imports": {"cv2": cv2.__version__, "numpy": numpy.__version__, "scipy": scipy.__version__,
                          "pandas": pandas.__version__, "sklearn": sklearn.__version__, "onnxruntime": onnxruntime.__version__}}
    engine.close()
    window = MainWindow()
    window.show()
    app.processEvents()
    assert window.events.columnCount() == 6
    result["workspace_constructed"] = True
    assert window.stage.mode == "A"
    for mode in "BCA":
        window.set_presentation(mode)
        app.processEvents()
    result["modern_presets_constructed"] = ["A", "B", "C"]
    window.close()
    ocr = TimestampOCR()
    result["local_ocr_loaded"] = True
    result["ocr_engine"] = ocr.signature
    if args.video:
        source = Path(args.video).resolve()
        before = digest_file(source)
        result["media_probe"] = probe_media(source)["format"].get("format_name")
        frame, actual = extract_frame(source, 0)
        report = ocr.recognize(frame)
        result["frame"] = {"size": frame.size, "actual_ms": actual, "ocr_timestamp": report.get("timestamp"), "success": report["success"]}
        assert report["success"], report
        assert digest_file(source) == before
        result["video_unchanged_sha256"] = before
    if args.json:
        source = Path(args.json).resolve()
        from cowmata_tailring.workspace.label_file import build_label_file, load_history, save_label_file
        from cowmata_tailring.workspace.work import SessionWork
        original = load_motion_json(source)
        work = SessionWork(digest_file(source))
        document = build_label_file(work, original, source.parent, [], {}, selection=(1000, 5000))
        document["source"]["project_root_hint"] = ""
        with tempfile.TemporaryDirectory(prefix="cowmata-portable-history-") as folder:
            label = Path(folder) / "portable.annotations.json"
            save_label_file(label, document, protected=[source])
            restored = load_history(label)
            expected = original.times_ms[(original.times_ms >= 1000) & (original.times_ms <= 5000)]
            assert numpy.array_equal(restored.motion.times_ms, expected)
            assert not restored.work.clock.anchors
            from cowmata_tailring.workspace.history_window import HistoryWindow
            history = HistoryWindow(label)
            history.future.result(timeout=20)
            history.poll_load()
            assert history.data.motion.sample_count == len(expected)
            history.close()
            result["independent_history_snippet"] = {"parent_start_ms": float(expected[0]), "samples": len(expected), "calibration_not_invented": True}
            full = build_label_file(work, original, source.parent, [], {})
            assert base64.b64decode(full["embedded_imu"]["original_json_base64"]) == source.read_bytes()
            full["source"]["project_root_hint"] = ""
            complete_label = Path(folder) / "complete.annotations.json"
            save_label_file(complete_label, full)
            full_history = load_history(complete_label)
            assert numpy.array_equal(full_history.motion.times_ms, original.times_ms)
            result["full_original_embedded"] = {"bytes_exact": True, "samples": full_history.motion.sample_count}
            from cowmata_tailring.workspace.event_models import available_packs, predict_one
            pack = available_packs(root)[0]
            result["event_models"] = []
            for model in pack["models"]:
                prediction = predict_one(pack, model, source, work.asset_id, "PORTABLE-TEST-ONLY", original.duration_ms, Path(folder) / "cache")
                result["event_models"].append({"model": model["id"], "candidates": len(prediction["candidates"]), "seconds": prediction["elapsed_s"],
                                               "clock_source": prediction["clock_source"], "private_runtime": pack["runtime"]})
        before = digest_file(source)
        motion = load_motion_json(source)
        result["imu"] = {"samples": len(motion.times_ms), "duration_ms": motion.duration_ms, "sha256": before}
        assert digest_file(source) == before
    with tempfile.TemporaryDirectory(prefix="cowmata-portable-selftest-") as name:
        catalog = Catalog(name)
        result["project_lock_writer"] = not catalog.readonly
        reader = Catalog(name)
        assert reader.readonly
        reader.close()
        catalog.close()
    atomic_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
