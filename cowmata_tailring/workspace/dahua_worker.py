"""Independent Dahua task process; cancellation and state never touch the old page."""

import json
import sys
import time
from contextlib import closing, nullcontext
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cowmata_tailring.workspace import dahua_tasks as tasks
from cowmata_tailring.workspace.storage import atomic_json


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main():
    job = Path(sys.argv[1]).resolve()
    request = tasks.read_json(job / "dahua-request.json")

    def cancelled():
        return (job / "dahua-cancel").exists()

    last = [0.0]

    def progress(current, total, message):
        now = time.monotonic()
        if now - last[0] > 0.5 or current == total:
            emit(dict(event="progress", current=current, total=total, message=message))
            last[0] = now

    try:
        from cowmata_tailring.workspace.classification_resources import (
            acquire_preparation_slot,
            limit_worker,
        )

        emit(dict(event="resources", budget=limit_worker()))
        action = request["action"]
        # Source discovery must not queue behind lengthy transcodes.
        admission = (
            nullcontext()
            if action in {"disks", "scan", "restore"}
            else closing(acquire_preparation_slot(cancelled, progress))
        )
        with admission:
            if action == "disks":
                result = dict(disks=tasks.disks())
            elif action == "scan":
                result = tasks.index_summary(tasks.scan(request, job, cancelled, progress))
            elif action == "restore":
                result = tasks.index_summary(tasks.read_json(job / "dahua-index.json"))
                result["options"] = tasks.read_json(job / "dahua-plan.json", {}).get("request", {})
            elif action == "previews":
                index = tasks.read_json(job / "dahua-index.json")
                result = dict(previews=[])
                for position, group in enumerate(request["groups"]):
                    tasks.check(cancelled)
                    rows = [
                        r for r in index["rows"] if r["group"] == group and r["status"] != "invalid"
                    ]
                    if not rows:
                        continue
                    try:
                        preview = tasks.prepare_preview(rows[0], index, job, cancelled)
                        row = dict(group=group, preview=preview)
                    except (OSError, ValueError, RuntimeError) as exc:
                        tasks.check(cancelled)
                        row = dict(group=group, error=str(exc))
                    result["previews"].append(row)
                    emit(dict(event="preview", row=row))
                    progress(position + 1, len(request["groups"]), "读取静态缩略图")
            elif action == "organize":
                result = tasks.organize(
                    request["options"],
                    job,
                    cancelled,
                    progress,
                    on_row=lambda row: emit(
                        dict(
                            event="row",
                            row={
                                k: v
                                for k, v in row.items()
                                if k in {"source", "target", "status", "message"}
                            },
                        )
                    ),
                )
            else:
                raise ValueError("未知视频准备操作")
        atomic_json(job / "dahua-result.json", result, backup=False)
        emit(dict(event="result", path=str(job / "dahua-result.json")))
        return 0
    except Exception as exc:
        result = dict(error=str(exc), paused=isinstance(exc, InterruptedError))
        atomic_json(job / "dahua-error.json", result, backup=False)
        emit(dict(event="error", **result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
