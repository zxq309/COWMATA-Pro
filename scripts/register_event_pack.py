"""Maintainer-only inventory of a reviewed, locally supplied model pack.

Does not fetch, train or alter the supplied algorithm files. Re-run with a new
version directory for future compatible CLI packs; do not replace old versions.
"""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = root / "pack.json"
    if manifest.exists():
        raise SystemExit("Inventory already exists; never silently replace a registered pack")
    specs = [
        ("stand_up", "起立", "STANDING_UP", "--raw", "--cow", "spot_s", "score", "result.csv"),
        ("lie_down", "卧倒", "LYING_DOWN", "--input", None, "around_s", "ranking_score", "result.csv"),
        ("urination", "排尿", "URINATION", "--input", "--cow-id", "event_time_s", "score", "result.csv"),
        ("tail_raise", "抬尾", "TAIL_RAISED", "--json", "--cow", "spot_s", "score", "event_hints.csv"),
        ("tail_wag", "甩尾", "TAIL_WAGGING", "--json", "--cow-id", "center_s", "score", "candidates.csv"),
    ]
    models = []
    for key, title, code, input_arg, cow_arg, time_column, score_column, output in specs:
        folder = root / title
        files = {}
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        models.append(dict(id=key, title=title, code=code, entry=title + "/predict.py", files=files,
                           input_arg=input_arg, cow_arg=cow_arg, time_column=time_column, score_column=score_column,
                           output=output, output_directory=output != "result.csv",
                           extra_args=["--threads", "2"] if key == "urination" else []))
    payload = dict(schema=1, version=root.name, adapter="csv-points-v1", runtime="model_runtime_20260906",
                   trusted_local_pack=True, models=models)
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    print(manifest)


if __name__ == "__main__":
    main()
