"""GUI-independent bridge to one owned, cancellable numerical worker."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.workspace.storage import atomic_json

_LOCK = threading.Lock()


def run_job(request, *, cancelled=lambda: False, progress_path=None):
    from cowmata_security.client import require
    require('behavior')
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("算法后台已有任务运行，请完成或取消后再试。")
    try:
        with tempfile.TemporaryDirectory(prefix="cowmata-algorithms-") as folder:
            folder = Path(folder)
            request = {**request, "result": str(folder / "result.json"),
                       "progress": str(progress_path or folder / "progress.json")}
            atomic_json(folder / "request.json", request)
            process = run_cancellable(
                [sys.executable, "-B", str(Path(__file__).with_name("worker.py")), str(folder / "request.json")],
                timeout=86400, cancelled=cancelled, cwd=folder,
                input_data=__import__('cowmata_security.worker',fromlist=['pipe_credentials']).pipe_credentials('behavior'))
            if process.returncode:
                raise RuntimeError(process.stderr.decode("utf-8", "replace")[-3000:] or "Algorithm worker failed")
            if cancelled():
                raise InterruptedError("Algorithm task cancelled")
            return json.loads((folder / "result.json").read_text(encoding="utf-8"))
    finally:
        _LOCK.release()
