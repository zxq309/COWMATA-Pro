"""4.4.5: start / stop the forward calving decider (5_正向决策器) next to the downloader.

The forward decider is its own package (``<drive>\\5_正向决策器\\产犊\\推理算法\\<version>``), separate
from the trainer (3_训练器) and the reverse decider (4_逆向决策器): it loads its own copy of the trainer's
model and runs ``calving.py watch``, inferring a 待产犊 cow again whenever new complete JSON files of its
wearing arrive, and writes ``<drive>\\5_正向决策器\\产犊\\前端对接\\<version>`` (风险等级.csv, 监测记录.csv,
监测报告 HTML, 监测总览.html). Downloading starts it; pausing the download or quitting the Annotator
stops it. Sites still on the pre-4.4.5 layout (``4_决策器\\产犊\\预测算法``) keep working. Missing package →
nothing happens.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .paths import APP_ROOT, _drives

ALGORITHMS = (("5_正向决策器", "产犊", "推理算法"), ("4_决策器", "产犊", "预测算法"))
OVERVIEWS = ((("5_正向决策器", "产犊", "前端对接"), "监测总览.html"), (("4_决策器", "产犊", "前端APP"), "*.html"))
_process = None


def _version_key(name):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", name)]


def _anchors(app_root):
    first = Path(Path(app_root).anchor)
    return [first] + [d for d in _drives() if d != first]


def algorithm_home(app_root=APP_ROOT):
    """Newest forward-decider package with its own runtime, on the app's drive first."""
    for parts in ALGORITHMS:
        for anchor in _anchors(app_root):
            base = anchor.joinpath(*parts)
            try:
                versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name), reverse=True)
            except OSError:
                continue
            for folder in versions:
                if (folder / "calving.py").is_file() and (folder / "runtime" / "python.exe").is_file():
                    return folder
    return None


def decision_app(app_root=APP_ROOT):
    """The newest risk-level overview (监测总览.html) written by the forward decider, if any."""
    for parts, pattern in OVERVIEWS:
        for anchor in _anchors(app_root):
            base = anchor.joinpath(*parts)
            try:
                versions = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name), reverse=True)
            except OSError:
                continue
            for folder in versions:
                found = sorted(folder.glob(pattern))
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
        log("未找到正向决策器（5_正向决策器\\产犊\\推理算法\\<版本>），只下载不推理")
        return False
    (home / ".cache" / "live").mkdir(parents=True, exist_ok=True)
    (home / ".cache" / "live" / "stop").unlink(missing_ok=True)
    stream = open(home / ".cache" / "live" / "watch.log", "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    _process = subprocess.Popen([str(home / "runtime" / "python.exe"), "-X", "utf8", str(home / "calving.py"), "watch"],
                                cwd=str(home), stdout=stream, stderr=subprocess.STDOUT, creationflags=flags, env=env)
    stream.close()
    log(f"实时推理已启动：{home.parent.parent.parent.name} {home.name}（新数据到达即重新推理 待产犊 的牛）")
    return True


def stop(log=lambda message: None, app_root=APP_ROOT, wait=0.5):
    """Ask the watcher to exit (stop file), then terminate it; results already written stay valid."""
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
        log("实时推理已停止")
