"""Detection CLI.

detect: one raw JSON -> result.csv (columns compatible with the COWMATA event
        pack adapter: ``event_time_s`` + ``score``).
scan:   many raw JSON files / folders -> one alarm table + per-cow hourly rate.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from .detector import detect_file, load_bundle




def active_model(models: Path) -> Path:
    ptr = models / "active.json"
    if not ptr.is_file():
        raise SystemExit(f"没有已激活的排尿模型：{ptr}；请先运行 python uri.py train")
    return models / "versions" / json.loads(ptr.read_text(encoding="utf-8"))["version"]


def _one(args):
    path, model_dir, thr = args
    try:
        det, info = detect_file(path, load_bundle(model_dir), thr)
        det.insert(0, "raw_path", str(path))
        return det, dict(raw_path=str(path), **{k: v for k, v in info.items() if k != "calibration"},
                         cal_suspect=info["calibration"].get("suspect"))
    except Exception as exc:
        return pd.DataFrame(), dict(raw_path=str(path), error=repr(exc))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["detect", "scan"])
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=None, help="模型版本目录；默认使用 models/active.json")
    ap.add_argument("--models", type=Path, default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--cow-id", default="")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--threads", type=int, default=None, help="兼容旧事件包参数，忽略")
    a = ap.parse_args(argv)
    if a.model is None and a.models is None:
        ap.error("--model or --models is required")
    model_dir = a.model or active_model(a.models)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    files = []
    for item in a.input:
        p = Path(item)
        files += sorted(p.rglob("*_raw.json")) if p.is_dir() else [p]
    if a.command == "detect" and len(files) == 1:
        det, info = _one((files[0], model_dir, a.threshold))
        if "error" in info:
            raise SystemExit(info["error"])
        det["cow_group"] = a.cow_id
        det.to_csv(a.output, index=False, encoding="utf-8-sig")
        print(json.dumps(dict(output=str(a.output), **info), ensure_ascii=False, default=str))
        return
    with ProcessPoolExecutor(max(1, a.workers)) as ex:
        res = list(ex.map(_one, [(f, model_dir, a.threshold) for f in files], chunksize=4))
    det = pd.concat([r[0] for r in res if len(r[0])], ignore_index=True) if any(len(r[0]) for r in res) else pd.DataFrame()
    info = pd.DataFrame([r[1] for r in res])
    det.to_csv(a.output, index=False, encoding="utf-8-sig")
    info.to_csv(a.output.with_name(a.output.stem + "_sessions.csv"), index=False, encoding="utf-8-sig")
    ok = info[info.get("error").isna()] if "error" in info else info
    hours = ok.valid_s.sum() / 3600 if len(ok) else 0
    print(json.dumps(dict(files=len(files), errors=int(len(info) - len(ok)), hours=round(hours, 1), detections=int(len(det)),
                          per_hour=round(len(det) / max(hours, 1e-9), 3), output=str(a.output)), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
