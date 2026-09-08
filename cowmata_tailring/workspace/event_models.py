"""Reviewed, versioned event CLI packs -> parent-IMU candidate points.

The GUI never imports legacy sklearn/pickle. Model wall-clock guesses are not
used: only the recording-relative point enters the workspace's clock mapping.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cowmata_tailring.media.subprocess_tools import run_cancellable

from .catalog import assert_not_being_written, digest_file, file_stamp
from .storage import atomic_json

APP_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = "csv-points-v1"
_LOAD_LOCK = threading.Lock()
_ACTIVE_INFERENCES = 0


def inference_active():
    with _LOAD_LOCK:
        return _ACTIVE_INFERENCES > 0


@contextmanager
def inference_load():
    global _ACTIVE_INFERENCES
    with _LOAD_LOCK:
        _ACTIVE_INFERENCES += 1
    try:
        yield
    finally:
        with _LOAD_LOCK:
            _ACTIVE_INFERENCES -= 1


def safe_child(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("Event pack path escapes its directory")
    return path


def available_packs(app_root=APP_ROOT):
    result = []
    for path in sorted((Path(app_root) / "assets/event_models").glob("*/pack.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or data.get("adapter") != ADAPTER or data.get("runtime") != "model_runtime_20260906":
            continue  # newer runtime contracts need a separately tested adapter
        result.append({**data, "root": path.parent, "hash": digest_file(path), "app_root": Path(app_root)})
    return result


def verify_model(pack, model):
    if model not in pack["models"] or model["entry"] not in model["files"]:
        raise ValueError("Unregistered event model")
    for relative, expected in model["files"].items():
        if digest_file(safe_child(pack["root"], relative)) != expected:
            raise ValueError("Event model file changed; restore or register a new reviewed pack: " + relative)
    runtime = safe_child(pack["app_root"], pack["runtime"] + "/python.exe")
    if not runtime.is_file():
        raise FileNotFoundError("Portable event runtime missing; extract the complete portable package")
    return runtime


def _csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _audit_value(value):
    # Empty model summaries can legitimately report NaN/Infinity statistics.
    # Preserve that unavailable status explicitly, never invent a numeric 0.
    if isinstance(value, float) and not math.isfinite(value):
        return {"unavailable_nonfinite": str(value)}
    if isinstance(value, dict):
        return {key: _audit_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_value(item) for item in value]
    return value


def normalize_output(folder, model, duration_ms):
    folder = Path(folder)
    rows = _csv(folder / model["output"])
    candidates = []
    for index, row in enumerate(rows):
        point = float(row[model["time_column"]]) * 1000
        score = float(row[model["score_column"]])
        if not math.isfinite(point) or not math.isfinite(score) or not 0 <= point <= duration_ms:
            raise ValueError("Model output contains invalid or out-of-recording candidate positions")
        candidates.append({"row": index, "code": model["code"], "point_ms": point, "score": score,
                           "review_status": "pending", "time_semantics": "approximate_point",
                           "quality": {k: row[k] for k in ("support_grade", "calibration_risk", "calibration_note", "quality_scope", "reference_clean") if k in row}})
    audit = {}
    for path in sorted(folder.iterdir()):
        if path.suffix == ".json":
            audit[path.name] = _audit_value(json.loads(path.read_text(encoding="utf-8-sig")))
        elif path.suffix == ".csv" and path.name != model["output"]:
            values = _csv(path)
            audit[path.name] = {"rows": len(values), "sha256": digest_file(path), "sample": values[:20]}
    return candidates, audit


def predict_one(pack, model, source, asset_id, cow_id, duration_ms, cache_dir, *, cancelled=lambda: False, force=False):
    source = Path(source).resolve()
    if cancelled():
        raise InterruptedError("Event inference cancelled")
    before = file_stamp(source)
    assert_not_being_written(source)
    if digest_file(source) != asset_id or file_stamp(source) != before:
        raise ValueError("IMU source changed; refresh before event inference")
    runtime = verify_model(pack, model)
    identity = dict(asset_id=asset_id, cow_id=cow_id, pack_sha256=pack["hash"], model_id=model["id"], adapter=ADAPTER)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(cache_dir) / (key + ".json")
    if cache.is_file() and not force:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            body = saved["result"]
            checksum = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if saved["sha256"] == checksum and body["identity"] == identity and body["id"] == key:
                return {**body, "cached": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # cache is replaceable; never overwrite human review data
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="cowmata-events-") as temporary:
        folder = Path(temporary)
        output = folder if model["output_directory"] else folder / model["output"]
        command = [str(runtime), "-I", "-B", str(Path(__file__).with_name("event_worker.py")),
                   str(safe_child(pack["root"], model["entry"])), model["input_arg"], str(source), "--output", str(output)]
        if model.get("cow_arg"):
            command += [model["cow_arg"], cow_id]
        command += model.get("extra_args", [])
        env = dict(os.environ, NUMBA_CACHE_DIR=str(folder / "numba"), PYTHONIOENCODING="utf-8")
        with inference_load():
            process = run_cancellable(command, timeout=1800, cancelled=cancelled, env=env, cwd=folder)
        if process.returncode:
            error = (process.stderr or process.stdout).decode("utf-8", "replace")[-3000:]
            raise RuntimeError("Event model failed (unknown, not a negative recording): " + error)
        candidates, audit = normalize_output(folder, model, duration_ms)
    assert_not_being_written(source)
    if before != file_stamp(source) or digest_file(source) != asset_id:
        raise ValueError("IMU source changed during inference; candidates discarded")
    if cancelled():
        raise InterruptedError("Event inference cancelled")
    for candidate in candidates:
        candidate["id"] = hashlib.sha256((key + ":" + str(candidate["row"])).encode()).hexdigest()
    result = {"id": key, "identity": identity, "version": pack["version"], "model_title": model["title"],
              "model_files": model["files"], "candidates": candidates, "audit": audit,
              "elapsed_s": time.monotonic() - started, "score_is_probability": False,
              "unreviewed_is_negative": False, "clock_source": "parent_imu_ms_only", "cached": False}
    checksum = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    atomic_json(cache, {"sha256": checksum, "result": result})
    return result
