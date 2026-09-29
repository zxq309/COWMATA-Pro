"""4.4.1: start / stop the calving decider (决策器) next to the downloader.

The decider is its own package (``<drive>\\4_决策器\\产犊\\预测算法\\<version>``), separate from the
trainer: it loads its own copy of the trainer's model and runs ``calving.py watch``, re-deciding a
待产犊 cow whenever new complete JSON files of its wearing arrive, and writes ``<drive>\\4_决策器\\产犊``
(decision CSVs + the terminal APP). Downloading starts it; pausing the download or quitting the
Annotator stops it. Missing package → nothing happens.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .paths import APP_ROOT, _drives

ALGORITHMS = ("4_决策器", "产犊", "预测算法")
DECISIONS = ("4_决策器", "产犊")
_process = None


def _version_key(name):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", name)]


def algorithm_home(app_root=APP_ROOT):
    """Newest decider package with its own runtime, on the app's drive first."""
    anchors = [Path(Path(app_root).anchor)] + [d for d in _drives() if d != Path(Path(app_root).anchor)]
    for anchor in anchors:
        base = anchor.joinpath(*ALGORITHMS)
        try:
            versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name), reverse=True)
        except OSError:
            continue
        for folder in versions:
            if (folder / "calving.py").is_file() and (folder / "runtime" / "python.exe").is_file():
                return folder
    return None


def decision_app(app_root=APP_ROOT):
    """The newest terminal APP written by the decider, if any."""
    for anchor in [Path(Path(app_root).anchor)] + _drives():
        base = anchor.joinpath(*DECISIONS, "前端APP")
        try:
            versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name), reverse=True)
        except OSError:
            continue
        for folder in versions:
            found = sorted(folder.glob("*.html"))
            if found:
                return found[0]
    return None


def running():
    return _process is not None and _process.poll() is None


def start(log=lambda message: None, app_root=APP_ROOT):
    """Launch ``calving.py watch`` once; a watcher already running elsewhere simply keeps the lock."""
    global _process
    if os.environ.get("COWMATA_DECIDER", "1") == "0":
        return False
    if running():
        return True
    home = algorithm_home(app_root)
    if home is None:
        log("未找到决策器（4_决策器\\产犊\\预测算法\\<版本>），只下载不决策")
        return False
    (home / ".cache" / "live").mkdir(parents=True, exist_ok=True)
    (home / ".cache" / "live" / "stop").unlink(missing_ok=True)
    stream = open(home / ".cache" / "live" / "watch.log", "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    _process = subprocess.Popen([str(home / "runtime" / "python.exe"), "-X", "utf8", str(home / "calving.py"), "watch"],
                                cwd=str(home), stdout=stream, stderr=subprocess.STDOUT, creationflags=flags, env=env)
    stream.close()
    log(f"实时决策已启动：{home.name}（新数据到达即重新决策 待产犊 的牛）")
    return True


def stop(log=lambda message: None, app_root=APP_ROOT, wait=0.5):
    """Ask the watcher to exit (stop file), then terminate it; decisions already written stay valid."""
    global _process
    home = algorithm_home(app_root)
    if home is not None and (running() or (home / ".cache" / "live" / "watch.lock").exists()):
        try:
            (home / ".cache" / "live" / "stop").write_text("stop", encoding="utf-8")
        except OSError:
            pass
    if _process is not None:
        try:
            _process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            _process.terminate()
        _process = None
        log("实时决策已停止")
