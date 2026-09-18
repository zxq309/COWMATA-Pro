"""Owned, cancellable helper processes. Never launch an interactive shell."""
from __future__ import annotations

import subprocess
import time


def run_cancellable(command, *, timeout=90, cancelled=None, env=None, cwd=None, input_data=None, stdout_file=None, stderr_file=None):
    process = subprocess.Popen(command, stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
                               stderr=stderr_file if stderr_file is not None else subprocess.PIPE,
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
                return subprocess.CompletedProcess(command, process.returncode, stdout or b'', stderr or b'')
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


def run_progress(command, *, progress=lambda _: None, timeout=3600, cancelled=None, stall_timeout=120):
    """Drain FFmpeg progress continuously; own and stop only this child process."""
    import collections
    import queue
    import threading

    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=int(getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    lines = queue.SimpleQueue()
    output, errors = [], collections.deque(maxlen=64)
    def read_stdout():
        try:
            for line in iter(process.stdout.readline, b''):
                lines.put(line)
        finally:
            lines.put(None)
    def read_stderr():
        for block in iter(lambda: process.stderr.read(65536), b''):
            errors.append(block)
    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in readers:
        thread.start()
    def activity():
        return None
    if sys_platform_windows():
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
        def activity():
            value = Counters()
            if kernel.GetProcessIoCounters(int(process._handle), ctypes.byref(value)):
                return value.read_bytes + value.write_bytes
            return None
    started = last_advance = time.monotonic()
    last_io = activity()
    fields, previous = {}, None
    ended = False
    try:
        while not ended or process.poll() is None:
            if cancelled and cancelled():
                raise RuntimeError('读取请求已取消')
            now = time.monotonic()
            if now - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            current_io = activity()
            # Small progress-pipe writes must not masquerade as media I/O.
            if current_io is not None and last_io is not None and current_io - last_io >= 65536:
                last_advance, last_io = now, current_io
            try:
                line = lines.get(timeout=.1)
            except queue.Empty:
                line = b''
            if line is None:
                ended = True
            elif line:
                output.append(line)
                key, sep, value = line.decode('utf-8', 'replace').strip().partition('=')
                if sep:
                    fields[key] = value
                if key == 'progress':
                    marker = tuple(fields.get(k) for k in ('frame', 'out_time_us', 'total_size'))
                    if marker != previous:
                        last_advance, previous = now, marker
                    progress(dict(fields))
                    fields.clear()
            if process.poll() is None and now - last_advance > stall_timeout:
                raise RuntimeError('媒体处理持续无进展，已停止本次尝试，可回退或重试')
        for reader in readers:
            reader.join(timeout=2)
        return subprocess.CompletedProcess(command, process.returncode, b''.join(output), b''.join(errors))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()


def sys_platform_windows():
    import os
    return os.name == 'nt'
