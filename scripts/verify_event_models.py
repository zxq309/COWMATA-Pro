"""Real-record parity against original CLIs; neither trains nor evaluates accuracy."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import digest_file
from cowmata_tailring.workspace.event_models import available_packs, normalize_output, predict_one
from cowmata_tailring.media.subprocess_tools import run_cancellable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, action="append", required=True)
    parser.add_argument("--cow", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference-python", type=Path)
    parser.add_argument("--original-models", type=Path)
    args = parser.parse_args()
    args.out = args.out.resolve()
    if len(args.cow) != len(args.raw):
        parser.error("One --cow per --raw is required")
    args.out.mkdir(parents=True, exist_ok=False)
    pack = available_packs()[0]
    original = args.original_models or pack["root"]
    source_hashes = {str(p): digest_file(p) for p in args.raw}
    model_hashes = {str(original / name): digest_file(original / name) for m in pack["models"] for name in m["files"]}
    assert all(model_hashes[str(original / name)] == expected for m in pack["models"] for name, expected in m["files"].items())
    report = {"results": [], "accuracy_evaluated": False}
    for n, (source, cow) in enumerate(zip(args.raw, args.cow)):
        motion = load_motion_json(source)
        for model in pack["models"]:
            started = time.monotonic()
            result = predict_one(pack, model, source, source_hashes[str(source)], cow, motion.duration_ms, args.out / "cache")
            item = {"source": str(source), "source_sha256": source_hashes[str(source)], "model": model["id"],
                    "candidates": len(result["candidates"]), "worker_seconds": result["elapsed_s"], "audit": result["audit"]}
            if args.reference_python:
                folder = args.out / (str(n) + "_reference_" + model["id"])
                folder.mkdir()
                output = folder if model["output_directory"] else folder / model["output"]
                command = [str(args.reference_python), "-B", str(original / model["entry"]), model["input_arg"], str(source.resolve()), "--output", str(output)]
                if model.get("cow_arg"):
                    command += [model["cow_arg"], cow]
                command += model["extra_args"]
                env = dict(os.environ, NUMBA_CACHE_DIR=str(folder / "numba"), PYTHONIOENCODING="utf-8",
                           OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2", NUMBA_NUM_THREADS="2")
                process = run_cancellable(command, timeout=1800, env=env, cwd=folder)
                if process.returncode:
                    raise RuntimeError(process.stderr.decode("utf-8", "replace"))
                expected, audit = normalize_output(folder, model, motion.duration_ms)
                actual = result["candidates"]
                assert len(actual) == len(expected), model["id"]
                for left, right in zip(actual, expected):
                    assert left["point_ms"] == right["point_ms"] and left["code"] == right["code"]
                    assert math.isclose(left["score"], right["score"], rel_tol=1e-10, abs_tol=1e-12)
                    assert left["quality"] == right["quality"]
                item["parity"] = True
                item["reference_audit"] = audit
            item["total_seconds"] = time.monotonic() - started
            report["results"].append(item)
            print(json.dumps({k: v for k, v in item.items() if "audit" not in k}, ensure_ascii=True), flush=True)
            (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert {p: digest_file(Path(p)) for p in source_hashes} == source_hashes
    assert {p: digest_file(Path(p)) for p in model_hashes} == model_hashes
    report["originals_unchanged"] = True
    report["passed"] = True
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
