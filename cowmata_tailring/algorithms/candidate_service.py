"""Service layer for unattended candidate generation.

Candidate generation never writes labels. It writes a review queue carrying
the source hash, suite hash and model version so the UI can route candidates
to CandidateWindow for human confirmation.
"""
from __future__ import annotations

import csv
from pathlib import Path

from cowmata_tailring.workspace.storage import atomic_json

from .adapter import predict_one
from .paths import ensure_runtime_layout
from .registry import read_suite


def run_candidate_scan(pack, records, output, cache_dir, *, cancelled=lambda: False, progress=lambda *_: None):
    suite = read_suite(pack["root"])
    selected = list(pack.get("models", suite["models"]))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    queue = []
    issues = []
    total = len(records)
    for index, record in enumerate(records):
        if cancelled():
            raise InterruptedError("Candidate scan cancelled")
        for model in selected:
            if cancelled():
                raise InterruptedError("Candidate scan cancelled")
            try:
                result = predict_one(pack, model, record["source"], record["asset_id"],
                                     record.get("cow_id", ""), int(record["duration_ms"]),
                                     cache_dir, cancelled=cancelled)
                for candidate in result["candidates"]:
                    queue.append({
                        **candidate,
                        "record_id": record.get("asset_id"),
                        "source": record.get("source"),
                        "cow_id": record.get("cow_id", ""),
                        "model_version": result["version"],
                        "model_code": model["code"],
                        "review_status": "pending",
                    })
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                issues.append({"source": record.get("source"), "code": model["code"], "reason": str(exc)})
        progress(index + 1, total, "生成行为候选并写入复核队列")
    payload = {
        "schema": "cowmata-candidate-review-1",
        "suite_version": suite["version"],
        "suite_sha256": pack["hash"],
        "records": total,
        "candidates": queue,
        "issues": issues,
        "requires_human_confirmation": True,
        "missing_is_negative": False,
    }
    atomic_json(output / "candidate-review-queue.json", payload)
    with (output / "candidate-review-queue.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["id", "model_code", "model_version", "cow_id", "source",
                  "start_ms", "point_ms", "end_ms", "score", "review_status"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(queue)
    atomic_json(output / "candidate-review-issues.json", {"issues": issues})
    return payload