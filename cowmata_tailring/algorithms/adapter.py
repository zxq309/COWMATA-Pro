"""Adapt numeric event suites to the existing human-review candidate contract."""
from __future__ import annotations

import hashlib
import json
import time

from cowmata_tailring.workspace.catalog import digest_file, file_stamp
from cowmata_tailring.workspace.storage import atomic_json

from .registry import APP_ROOT, active_suite, read_suite
from .runner import run_job

ADAPTER = "numeric-event-intervals-1"


def available_pack():
    suite = active_suite()
    if suite is None:
        return None
    return {**suite, "adapter": ADAPTER, "app_root": APP_ROOT, "runtime": "runtime",
            "models": [{**m, "id": m["code"].lower(), "files": {m["file"]: m["sha256"]}}
                       for m in suite["models"]]}


def predict_one(pack, model, source, asset_id, cow_id, duration_ms, cache_dir, *, cancelled=lambda: False, force=False):
    from pathlib import Path
    suite = read_suite(pack["root"])
    if suite["hash"] != pack["hash"]:
        raise ValueError("Algorithm version changed; refresh before scanning")
    entry = next((m for m in suite["models"] if m["code"] == model["code"]), None)
    if entry is None or entry["sha256"] != model["sha256"]:
        raise ValueError("Unregistered numeric event model")
    source = Path(source).resolve()
    before = file_stamp(source)
    if cancelled():
        raise InterruptedError("Event inference cancelled")
    if digest_file(source) != asset_id or before != file_stamp(source):
        raise ValueError("Original data changed before algorithm inference")
    identity = dict(asset_id=asset_id, cow_id=cow_id, pack_sha256=pack["hash"],
                    model_id=model["id"], adapter=ADAPTER)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_dir = Path(cache_dir)
    cache = cache_dir / (key+".json")
    if cache.is_file() and not force:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            result = saved["result"]
            digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if saved["sha256"] == digest and result["identity"] == identity and result["id"] == key:
                return {**result, "cached": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    started = time.monotonic()
    output = run_job(dict(action="predict", suite=str(pack["root"]), code=model["code"],
                          record=dict(raw=str(source), asset_id=asset_id), cache=str(cache_dir/"features")),
                     cancelled=cancelled)
    if before != file_stamp(source) or digest_file(source) != asset_id or cancelled():
        raise ValueError("Source changed or inference cancelled; results discarded")
    candidates = output["candidates"]
    for i, candidate in enumerate(candidates):
        if not 0 <= candidate["start_ms"] <= candidate["point_ms"] <= candidate["end_ms"] <= duration_ms:
            raise ValueError("Model output outside original record")
        candidate["row"] = i
        candidate["id"] = hashlib.sha256((key+":"+str(i)).encode()).hexdigest()
    result = dict(id=key, identity=identity, version=pack["version"], model_title=model["title"],
                  model_files=model["files"], candidates=candidates, audit=output["audit"],
                  elapsed_s=time.monotonic()-started, score_is_probability=False,
                  unreviewed_is_negative=False, clock_source="parent_imu_ms_only", cached=False)
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    atomic_json(cache, {"sha256": digest, "result": result})
    return result
