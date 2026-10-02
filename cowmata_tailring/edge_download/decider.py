"""4.4.6: start / stop the forward calving deciders (5_正向决策器) next to the downloader.

Each algorithm route of the site tree has its own forward decider package:
``<目录树>\\常规经典算法\\5_正向决策器\\产犊\\<版本>\\推理算法`` and ``<目录树>\\AI大模型算法\\5_正向决策器\\产犊\\<版本>\\推理算法``
(the 4.4.5 layouts ``<目录树>\\5_正向决策器\\产犊\\…`` and the 4.4.1 ``4_决策器\\产犊\\预测算法\\<版本>`` still work as the
classical route). A package loads its own model, runs ``calving.py watch`` — inferring a 待产犊 cow again whenever
new complete JSON files of its wearing arrive — and writes ``…\\<版本>\\前端对接`` (风险等级.csv, 监测记录.csv, 监测报告
HTML, 监测总览.html). Which routes start with 下载 is chosen per computer in 下载设置 (``forward_tracks``): e.g. the
classical forward decider on the production computer, the large-model one on the computer serving the model.
Pausing the download or quitting the Annotator stops them. The tree the app keeps its data in is searched first,
then the other local drives; the newest version wins. Missing package → nothing happens.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .paths import APP_ROOT, CLASSIC, FORWARD, LLM, TRACKS, _drives, tree_root, version_key

LEGACY = "4_决策器"
# Per route: (folders before the version folder, folders after it), newest layout first.
LAYOUTS = {
    CLASSIC: (((CLASSIC, FORWARD, "产犊"), ("推理算法",)), ((FORWARD, "产犊"), ("推理算法",)),
              ((FORWARD, "产犊", "推理算法"), ()), ((LEGACY, "产犊", "预测算法"), ())),
    LLM: (((LLM, FORWARD, "产犊"), ("推理算法",)),),
}
OVERVIEWS = {
    CLASSIC: (((CLASSIC, FORWARD, "产犊"), ("前端对接",), "监测总览.html"), ((FORWARD, "产犊"), ("前端对接",), "监测总览.html"),
              ((FORWARD, "产犊", "前端对接"), (), "监测总览.html"), ((LEGACY, "产犊", "前端APP"), (), "*.html")),
    LLM: (((LLM, FORWARD, "产犊"), ("前端对接",), "监测总览.html"),),
}
DEFAULT_TRACKS = (CLASSIC,)
_processes = {}


def _anchors(app_root):
    """The data tree first, then the app's drive and every other local drive."""
    found = []
    for anchor in [tree_root(app_root), Path(Path(app_root).resolve().anchor), *_drives()]:
        if anchor is not None and anchor not in found:
            found.append(anchor)
    return found


def _versions(base):
    try:
        return [p for p in base.iterdir() if p.is_dir() and version_key(p.name)]
    except OSError:
        return []


def _tracks(tracks):
    chosen = [t for t in (tracks if tracks is not None else DEFAULT_TRACKS) if t in TRACKS]
    return chosen


def algorithm_home(app_root=APP_ROOT, track=CLASSIC):
    """Newest forward-decider package (with its own runtime) of one route, in the data tree first."""
    for anchor in _anchors(app_root):
        found = []
        for before, after in LAYOUTS[track]:
            for version in _versions(anchor.joinpath(*before)):
                folder = version.joinpath(*after)
                if (folder / "calving.py").is_file() and (folder / "runtime" / "python.exe").is_file():
                    found.append((version_key(version.name), folder))
        if found:
            return max(found, key=lambda pair: pair[0])[1]
    return None


def decision_app(app_root=APP_ROOT, tracks=None):
    """The newest risk-level overview (监测总览.html) of the chosen routes (any route when none has one)."""
    order = _tracks(tracks) + [t for t in TRACKS if t not in _tracks(tracks)]
    for track in order:
        for anchor in _anchors(app_root):
            found = []
            for before, after, pattern in OVERVIEWS[track]:
                for version in _versions(anchor.joinpath(*before)):
                    pages = sorted(version.joinpath(*after).glob(pattern))
                    if pages:
                        found.append((version_key(version.name), pages[0]))
            if found:
                return max(found, key=lambda pair: pair[0])[1]
    return None


def _label(home):
    """'常规经典算法 5_正向决策器 4.4.6' for log lines, whatever the layout."""
    version = next((p.name for p in (home, *home.parents) if version_key(p.name)), home.name)
    track = next((p.name for p in home.parents if p.name in TRACKS), CLASSIC)
    area = next((p.name for p in home.parents if p.name in (FORWARD, LEGACY)), FORWARD)
    return f"{track} {area} {version}"


def running(track=None):
    tracks = [track] if track else list(_processes)
    return any(_processes.get(t) is not None and _processes[t].poll() is None for t in tracks)


def start(log=lambda message: None, app_root=APP_ROOT, tracks=None):
    """Launch ``calving.py watch`` of every chosen route once; a watcher already running elsewhere keeps its lock."""
    if os.environ.get("COWMATA_DECIDER", "1") == "0":
        return False
    started = False
    chosen = _tracks(tracks)
    if not chosen:
        log("下载设置里没有选择随下载启动的正向决策器，只下载不推理")
        return False
    for track in chosen:
        if running(track):
            started = True
            continue
        home = algorithm_home(app_root, track)
        if home is None:
            log(f"未找到正向决策器（目录树的 {track}\\5_正向决策器\\产犊\\<版本>\\推理算法），{track} 不推理")
            continue
        (home / ".cache" / "live").mkdir(parents=True, exist_ok=True)
        (home / ".cache" / "live" / "stop").unlink(missing_ok=True)
        stream = open(home / ".cache" / "live" / "watch.log", "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        _processes[track] = subprocess.Popen(
            [str(home / "runtime" / "python.exe"), "-X", "utf8", str(home / "calving.py"), "watch"],
            cwd=str(home), stdout=stream, stderr=subprocess.STDOUT, creationflags=flags, env=env)
        stream.close()
        log(f"实时推理已启动：{_label(home)}（新数据到达即重新推理 待产犊 的牛）")
        started = True
    return started


def stop(log=lambda message: None, app_root=APP_ROOT, wait=0.5):
    """Ask every watcher to exit (stop file), then terminate it; results already written stay valid."""
    for track in TRACKS:
        home = algorithm_home(app_root, track)
        if home is not None and (running(track) or (home / ".cache" / "live" / "watch.lock").exists()):
            try:
                (home / ".cache" / "live" / "stop").write_text("stop", encoding="utf-8")
            except OSError:
                pass
    for track, process in list(_processes.items()):
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            process.terminate()
        del _processes[track]
        log(f"实时推理已停止：{track}")
