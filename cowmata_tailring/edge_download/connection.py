"""Reuse an already authorized tunnel; never embed or invent a user's key."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .core import Cancelled, DownloadError

TASK_NAME = "CowmataRawDownloadTunnel-18031"

def health(base_url, timeout=3):
    with build_opener(ProxyHandler({})).open(base_url.rstrip("/") + "/health", timeout=timeout) as response:
        body = json.loads(response.read(65536))
        return body.get("code") == 0 and body.get("data", {}).get("ok") is True

def ensure_connection(base_url, cancel, log=lambda text: None):
    if cancel.is_set():
        raise Cancelled()
    address = urlsplit(base_url)
    if (os.name != "nt" or address.scheme != "http"
            or address.hostname not in {"127.0.0.1", "localhost"}
            or address.port != 18031 or address.path not in {"", "/"}):
        return
    try:
        if health(base_url):
            return
    except (OSError, ValueError):
        pass
    log("正在连接3090：唤起已授权的本机下载通道…")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/schtasks.exe"
    command = subprocess.Popen([str(exe), "/Run", "/TN", TASK_NAME],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cancel.wait(.25):
                raise Cancelled()
            status = command.poll()
            if status is not None and status != 0:
                raise DownloadError("这台电脑还没有可用的3090下载授权/通道，请由管理员配置；无需填写牛号或设备号。")
            try:
                with socket.create_connection((address.hostname, address.port), timeout=1):
                    pass
                if health(base_url, timeout=2):
                    log("3090下载通道已就绪")
                    return
            except (OSError, ValueError):
                pass
        raise DownloadError("3090通道暂未连通，自动模式会在下一轮重试；请检查网络或服务器状态。")
    finally:
        # Only own the short task-start command. The shared authorized SSH must continue.
        if command.poll() is None:
            command.kill()
        command.wait(timeout=3)
