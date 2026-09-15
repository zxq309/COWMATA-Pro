"""Motion access: reuse the tested task, or use Ledger's physical-interface SSH route."""

import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .connection import ensure_connection, health
from .core import Cancelled, DownloadError
from .ledger_direct import DirectBridge, clean_environment


@contextmanager
def raw_connection(values, cancel, log, connector=ensure_connection):
    if values.get('raw_connection') == 'http':
        yield
        return
    if values.get("raw_connection", "authorized_task") == "authorized_task":
        connector(values["server"], cancel, log)
        yield
        return
    url = urlsplit(values["server"])
    if (
        url.scheme != "http"
        or url.hostname not in ("127.0.0.1", "localhost")
        or not url.port
        or url.path not in ("", "/")
    ):
        raise DownloadError("直连 SSH 的九轴接口应为 http://127.0.0.1:本机端口")
    key = Path(values.get("raw_key", ""))
    if not key.is_file():
        raise DownloadError(
            "请在服务器连接设置中选择已授权的九轴 SSH 私钥；台账上传授权不提供九轴转发权限"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values.get("raw_user", "")):
        raise DownloadError("九轴 SSH 用户名无效")
    remote_port = int(values.get("raw_remote_port", 8031))
    if not 1 <= remote_port <= 65535:
        raise DownloadError("九轴远端接口端口无效")
    if cancel.is_set():
        raise Cancelled()
    app_root = Path(__file__).resolve().parents[2]
    executable = app_root / "vendor/ledger-ssh/usr/bin/ssh.exe"
    hosts = Path(__file__).with_name("ledger_known_hosts")
    with DirectBridge(values["ledger_host"], int(values["ledger_port"])) as bridge:
        alias = "[" + values["ledger_host"] + "]:" + str(values["ledger_port"])
        args = [
            str(executable),
            "-F",
            "none",
            "-N",
            "-o",
            "ProxyCommand=none",
            "-o",
            "ProxyJump=none",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "HostKeyAlias=" + alias,
            "-o",
            "UserKnownHostsFile=" + hosts.name,
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=2",
            "-L",
            f"127.0.0.1:{url.port}:127.0.0.1:{remote_port}",
            "-i",
            str(key),
            "-p",
            str(bridge.port),
            values["raw_user"] + "@127.0.0.1",
        ]
        process = subprocess.Popen(
            args,
            cwd=hosts.parent,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if cancel.wait(0.25):
                    raise Cancelled()
                if process.poll() is not None:
                    raise DownloadError("九轴 SSH 转发未建立：检查授权、服务器权限或本机端口占用")
                try:
                    ready = health(values["server"], timeout=1)
                except (OSError, ValueError):
                    ready = False
                if ready:
                    log("九轴通过真实网卡直连 SSH，接口已就绪")
                    yield
                    return
            raise DownloadError("九轴 SSH 已启动但接口未就绪，稍后自动重试")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=3)
