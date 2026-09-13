"""Private child process: directory work never holds the GUI's Python GIL."""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cowmata_tailring.workspace import organization as core


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main():
    job = core.safe_path(sys.argv[1])
    request = json.loads((job / "request.json").read_text(encoding="utf-8"))
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)  # This child only: BELOW_NORMAL.
    def cancelled():
        return (job / "cancel").exists()
    last = 0.0

    def progress(current, total, path):
        nonlocal last
        when = time.monotonic()
        if when - last >= .2 or total and current == total:
            emit({"event": "progress", "current": current, "total": total, "path": path})
            last = when

    def on_row(row):
        emit({'event': 'row', 'row': row})

    try:
        action = request["action"]
        if action == "audit":
            result = core.audit(request["roots"], cancelled, progress)
        elif action == 'organize':
            from cowmata_tailring.workspace.video_intake import organize
            result = organize(request['target'], request['sources'], request.get('start',''), request.get('end'),
                request.get('note',''), cancelled, progress, job=job, on_row=on_row,
                category=request.get('category'), farm=request.get('farm',''), cache=request.get('cache'),
                transfer=request.get('transfer','copy'), scenario=request.get('scenario','mixed'), workers=request.get('workers',4))
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
        with (job / "report.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            fields = ["source", "target", "kind", "device", "size", "status", "message",
                      "source_folder", "device_id", "cow_id", "field_mark", "record_date",
                      "record_start_ms", "suggested_folder", "identity_provenance"]
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(result.get("rows", []))
        emit({"event": "result", "path": str(job / "result.json")})
        return 0
    except Exception as exc:
        (job / "error.json").write_text(json.dumps({"error": str(exc), "paused": isinstance(exc, InterruptedError)},
                                                    ensure_ascii=False), encoding="utf-8")
        emit({"event": "error", "message": str(exc), "paused": isinstance(exc, InterruptedError)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
