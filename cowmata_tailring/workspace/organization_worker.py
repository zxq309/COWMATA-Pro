"""Private child process: directory work never holds the GUI's Python GIL."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cowmata_tailring.workspace import organization as core

_output_lock = threading.Lock()

def emit(value):
    with _output_lock:
        print(json.dumps(value, ensure_ascii=True), flush=True)


def main():
    job = core.safe_path(sys.argv[1])
    request = json.loads((job / "request.json").read_text(encoding="utf-8"))
    from cowmata_tailring.workspace.classification_resources import limit_worker
    emit({'event': 'resources', 'budget': limit_worker()})
    def cancelled():
        return (job / "cancel").exists()
    last = {}
    from cowmata_tailring.workspace.classification_report import LiveReport
    def on_report(path, revision):
        emit({'event': 'snapshot', 'path': path, 'revision': revision})
    live = LiveReport(job, on_publish=on_report) if request['action'] != 'organize' else None

    def progress(current, total, path):
        when = time.monotonic()
        parts = str(path).split(' · ', 2)
        key = parts[1] if len(parts) == 3 else 'scan'
        if when - last.get(key, 0) >= 1.0 or total and current == total:
            emit({"event": "progress", "current": current, "total": total, "path": path, "unit": "bytes" if str(path).startswith(("快速复制", "校验目标")) else "files"})
            last[key] = when

    def on_row(row):
        if request.get('action') != 'organize':
            live.row(row)
        if request.get('action') != 'organize':
            emit({'event': 'row', 'row': row})

    try:
        from cowmata_tailring.workspace.classification_resources import acquire_preparation_slot
        acquire_preparation_slot(cancelled, progress)
        action = request["action"]
        if action == "audit":
            result = core.audit(request["roots"], cancelled, progress)
        elif action == 'organize':
            from cowmata_tailring.workspace.video_intake import organize
            result = organize(request['target'], request['sources'], request.get('start',''), request.get('end'),
                request.get('note',''), cancelled, progress, job=job, on_row=on_row, on_report=on_report,
                category=request.get('category'), farm=request.get('farm',''), cache=request.get('cache'),
                transfer=request.get('transfer','copy'), scenario=request.get('scenario','mixed'), workers=request.get('workers',4),
                delete_unusable=request.get('delete_unusable', False))
        elif action == "import":
            from cowmata_tailring.workspace.resource_import import plan_import
            result = plan_import(request["target"], request["sources"], request["start"], request.get("end"),
                                      request.get("note", ""), cancelled, progress, category=request.get("category"), farm=request.get("farm", "扬大_高邮牧场"), cache=request.get("cache"), transfer=request.get("transfer", "copy"),scenario=request.get('scenario','mixed'),
                                      fast_video=request.get('fast_video', False), workers=request.get('workers', 4), on_row=on_row,
                                      video_suffix_only=request.get('fast_video', False))
        elif action == "normalize":
            result = core.plan_normalize(request["target"], cancelled, progress)
        elif action == "quarantine":
            result = core.plan_quarantine(request["report"], request["target"])
        elif action == "execute":
            plan = json.loads((job / "plan.json").read_text(encoding="utf-8"))
            if plan.get('fast_video'):
                from cowmata_tailring.workspace.resource_import import execute
                result = execute(plan, job, cancelled, progress, on_row=on_row)
                for row in result['rows']:
                    if row['status'] in {'ready', 'existing'}:
                        row['status'] = 'deleted' if row.get('operation') == 'delete_nonvideo' else 'done'
            else:
                result = core.execute(plan, job, cancelled, progress)
        else:
            raise ValueError("Unknown organization operation")
        if action not in {"audit", "execute", "organize"}:
            (job / "plan.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (job / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        if action not in {'organize', 'execute'}:
            for row in result.get('rows', []):
                live.row(row)
        emit({"event": "result", "path": str(job / "result.json")})
        return 0
    except Exception as exc:
        (job / "error.json").write_text(json.dumps({"error": str(exc), "paused": isinstance(exc, InterruptedError)},
                                                    ensure_ascii=False), encoding="utf-8")
        emit({"event": "error", "message": str(exc), "paused": isinstance(exc, InterruptedError)})
        return 2
    finally:
        if live:
            live.finish('paused' if cancelled() else 'completed')
        from cowmata_tailring.workspace.maintenance import cleanup_session
        cleanup_session()


if __name__ == "__main__":
    raise SystemExit(main())
