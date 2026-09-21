"""Independent Dahua task process; cancellation and state never touch the old page."""

import json
import sys
import threading
import time
from contextlib import closing, nullcontext
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cowmata_tailring.workspace import dahua_tasks as tasks
from cowmata_tailring.workspace.storage import atomic_json

_emit_lock = threading.Lock()


def emit(value):
    with _emit_lock:
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
                restored = tasks.pending_video_job(request.get("target")) or job
                result = tasks.index_summary(tasks.read_json(restored / "dahua-index.json"))
                result["job"] = str(restored)
                result["options"] = tasks.read_json(restored / "dahua-plan.json", {}).get("request", {})
                result["run"] = tasks.read_json(restored / "dahua-run.json", {})
            elif action == "wipe_survey":
                from cowmata_tailring.workspace.dahua_wipe import survey
                result = dict(survey=survey(int(request["number"])))
            elif action == "wipe":
                from cowmata_tailring.workspace.dahua_wipe import wipe

                def wipe_progress(current, total, label):
                    emit(dict(event="progress", current=current, total=total,
                              message="清盘中 · " + label))

                result = dict(wipe=wipe(int(request["number"]), request["identity"],
                                        wipe_progress, cancelled))
            elif action == "previews":
                from cowmata_tailring.workspace.dahua_run import configure_storage, release_media
                from cowmata_tailring.workspace.data_category import category_root
                from cowmata_tailring.workspace.farm_layout import storage_root
                if request.get("target"):
                    farm = Path(request["target"]).resolve()
                    configure_storage(job, storage_root(category_root(farm, farm.name, request["category"])))
                elif request.get("groups"):
                    raise ValueError("请先选择输出牧场，预览视频将使用目标盘")
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
                    if rows:
                        release_media(job, rows[0]["id"], normalized_only=True)
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
                        dict(event="resume", job=row["job"])
                        if row.get("event_kind") == "task_resume" else dict(
                            event="row",
                            row={
                                k: v
                                for k, v in row.items()
                                if k in {"event_kind", "source_id", "source", "target", "targets", "owner",
                                         "status", "phase", "message", "size", "method", "record_start_ms",
                                         "started_at", "finished_at", "file_seconds", "read_seconds",
                                         "convert_seconds", "verify_seconds", "archive_seconds",
                                         "existing_verified", "transfer_seconds", "media_percent", "frames",
                                         "fps", "media_speed", "output_bytes"}
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
    finally:
        from cowmata_tailring.workspace.maintenance import cleanup_session
        cleanup_session()


if __name__ == "__main__":
    raise SystemExit(main())
