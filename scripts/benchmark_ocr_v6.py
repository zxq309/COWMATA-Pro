"""Compare actual first frames and an unresolved tail, without source mutations."""
import argparse
import json
import socket
import time
from pathlib import Path

from cowmata_tailring.media.timeline import MediaTimelineIndex
from cowmata_tailring.workspace.catalog import digest_file
from cowmata_tailring.workspace.ocr import TimestampOCR
from cowmata_tailring.workspace.probe import extract_frame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--backend", choices=["v6", "legacy"], default="v6")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    # Block Python network access throughout initialization and inference.
    def deny(*_args, **_kwargs):
        raise RuntimeError("OCR attempted a network connection")
    socket.socket.connect = deny
    socket.create_connection = deny
    engine = TimestampOCR(backend=args.backend)
    starts = ["11:34:57", "11:34:52", "11:51:05", "12:07:18", "12:23:32"]
    cases = [(args.workspace / f"hiv{i:05}.mp4", 0, "2026-08-03 " + stamp, None) for i, stamp in enumerate(starts)]
    fixture = args.workspace / "workspace_validation_20260906" / "project_eight"
    cases.extend((fixture / f"视角{i}" / "批次1" / "001.mp4", 0, "2026-08-03 12:00:00", None) for i in range(1, 5))
    report_path = next((args.workspace / "workspace_validation_20260906" / "refined_probe").glob("hiv00001.*.probe.json"))
    timeline = MediaTimelineIndex.from_dict(json.loads(report_path.read_text(encoding="utf-8"))["timeline"])
    cases.append((args.workspace / "hiv00001.mp4", timeline.duration_ms - 100, None, timeline))
    rows = []
    for index, (source, target, expected, timeline) in enumerate(cases):
        print(json.dumps({"case": index, "stage": "extract", "backend": args.backend}), flush=True)
        initial_sha = digest_file(source)
        frame, actual = extract_frame(source, target, timeline)
        frame.save(args.out / f"{index:02}.png")
        start = time.perf_counter()
        report = engine.recognize(frame, filename=f"{source.name}@{actual:.3f}")
        elapsed = time.perf_counter() - start
        unchanged = initial_sha == digest_file(source)
        assert unchanged
        row = {"file": str(source), "actual_ms": actual, "expected": expected,
               "timestamp": report["timestamp"], "success": report["success"], "seconds": elapsed,
               "correct": report["timestamp"] == expected if expected else None,
               "source_unchanged": unchanged, "ocr": report}
        rows.append(row)
        (args.out / f"{index:02}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k != "ocr"}, ensure_ascii=True), flush=True)
    (args.out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
