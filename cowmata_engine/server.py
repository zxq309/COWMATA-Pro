"""Minimal local HTTP JSON service for an external front-end (stdlib only, localhost by default).

``POST /api``            body = request JSON (``{"action": ...}``) → response envelope
``POST /api/<action>``   body = request fields without ``action``
``GET  /api/info``       engine info
``GET  /api/jobs/<id>``  progress / result of a background job (``"async": true`` requests)
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import handle

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_BODY = 64 * 1024 * 1024


def _run_job(job_id, request):
    def progress(done, total, message):
        with _LOCK:
            _JOBS[job_id].update(done=done, total=total, message=message)

    response = handle(request, progress=progress, cancelled=lambda: _JOBS[job_id].get("cancel", False))
    with _LOCK:
        _JOBS[job_id].update(state="finished", response=response)


class Handler(BaseHTTPRequestHandler):
    server_version = "COWMATAEngine/4.3.3"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return

    def do_GET(self):
        if self.path in ("/api", "/api/info"):
            return self._send(200, handle(dict(action="engine.info")))
        if self.path.startswith("/api/jobs/"):
            job = _JOBS.get(self.path.rsplit("/", 1)[1])
            return self._send(200 if job else 404, job or dict(ok=False, error="任务不存在"))
        return self._send(404, dict(ok=False, error="未知路径"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, dict(ok=False, error="请求过大"))
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8-sig") or "{}")
        except ValueError:
            return self._send(400, dict(ok=False, error="请求不是 JSON"))
        if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
            job = _JOBS.get(self.path.split("/")[3])
            if job:
                job["cancel"] = True
            return self._send(200 if job else 404, dict(ok=bool(job)))
        if self.path.startswith("/api/") and self.path != "/api/":
            request = dict(request, action=self.path[len("/api/"):])
        if request.pop("async", False):
            job_id = uuid.uuid4().hex
            with _LOCK:
                _JOBS[job_id] = dict(id=job_id, state="running", action=request.get("action"), done=0, total=0, message="")
            threading.Thread(target=_run_job, args=(job_id, request), daemon=True).start()
            return self._send(202, dict(ok=True, job=job_id))
        response = handle(request)
        return self._send(200 if response["ok"] else 400, response)


def make_server(host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), Handler)


def serve(host="127.0.0.1", port=8765):
    server = make_server(host, port)
    print(f"COWMATA engine listening on http://{host}:{server.server_address[1]}/api", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
