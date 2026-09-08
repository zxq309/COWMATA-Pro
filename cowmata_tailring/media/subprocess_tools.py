"""Owned, cancellable helper processes. Never launch an interactive shell."""
from __future__ import annotations

import subprocess
import time


def run_cancellable(command, *, timeout=90, cancelled=None, env=None, cwd=None, input_data=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.PIPE if input_data is not None else None,
                               env=env, cwd=cwd,
                               creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    started = time.monotonic()
    try:
        while True:
            if cancelled and cancelled():
                raise RuntimeError("读取请求已取消")
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(input=input_data, timeout=.25)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                input_data = None  # communicate retains its partially written buffer.
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
