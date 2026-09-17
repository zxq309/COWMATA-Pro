"""Read-only real-media integration probe; writes evidence only to --out."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_tailring.workspace.catalog import digest_file, file_stamp
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for value in args.files:
        path = Path(value).resolve()
        inspector = SourceInspector(path.parent, out, progress=lambda s: print(s, flush=True))
        started = time.perf_counter()
        before = file_stamp(path)
        asset_id = digest_file(path)
        metadata = inspector(path, "imu" if path.suffix == ".json" else "video", asset_id)
        assert file_stamp(path) == before, "Original source changed"
        report_path = out / (path.stem + "." + asset_id[:12] + ".probe.json")
        atomic_json(report_path, metadata)
        result = {"file": str(path), "asset_id": asset_id, "probe_report": str(report_path), "seconds": time.perf_counter() - started,
                  "duration_ms": metadata["duration_ms"], "warnings": metadata["warnings"],
                  "intervals": len(metadata.get("intervals", [])),
                  "samples": [{"media_ms": s["media_ms"], "wall_ms": s.get("wall_ms"),
                               "timestamp": s.get("ocr", {}).get("timestamp"), "error": s.get("error")}
                              for s in metadata.get("samples", [])] if isinstance(metadata.get("samples"), list) else metadata["samples"]}
        results.append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        atomic_json(out / "summary.json", results)


if __name__ == "__main__":
    main()
