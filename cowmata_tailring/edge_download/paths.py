"""Data locations: the site directory tree (目录树), never a fixed drive (4.4.6).

The site tree is one folder — usually a drive root such as ``E:\\`` — holding, in workflow order::

    1_下载器\\<牧场>\\                       every data file: category folders (端侧下载), 台账 (download mirror
                                           and the 上传 working copy), 标注工程, 科牧特_协作标注 (派包 / 回传 / 接收)
    2_标注器\\<版本>\\                       this app's release packages (installer, portable, source)
    常规经典算法\\3_训练器\\产犊\\<版本>\\      算法 · 数据集 · 模型 · 训练结果
    常规经典算法\\4_逆向决策器\\产犊\\<版本>\\   决策算法 · 决策结果
    常规经典算法\\5_正向决策器\\产犊\\<版本>\\   推理算法 · 前端对接
    AI大模型算法\\3_训练器 | 4_逆向决策器 | 5_正向决策器\\…   the same structure for the local large-model route

(4.4.5 kept 3_训练器 … 5_正向决策器 directly in the tree; that layout is still recognised.)

Locations inside the tree are saved relative to it (``目录树/1_下载器/<牧场>``) and resolved against the
tree found at run time, so moving the whole tree to another drive, or copying it to another computer,
needs no setting change. The tree is the one this app runs inside (portable package unpacked under
2_标注器), otherwise the most complete tree on the local drives. A saved tree location whose tree is not
plugged in is an error — data is never silently written somewhere else.
"""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath


def documents_root() -> Path:
    """Return the Windows user's Documents directory without requiring a drive."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            path = Path(os.path.expandvars(value))
            if path.is_absolute() and Path(path.anchor).is_dir():
                return path
        except (OSError, TypeError, ValueError):
            pass
    profile = os.environ.get("USERPROFILE")
    return (Path(profile) if profile else Path.home()) / "Documents"


# Resolve against the application, never the shortcut's working directory.
APP_ROOT = Path(__file__).resolve().parents[2]

# The site tree, in workflow order.
DOWNLOADS = "1_下载器"
ANNOTATOR = "2_标注器"
TRAINER = "3_训练器"
REVERSE = "4_逆向决策器"
FORWARD = "5_正向决策器"
TREE = (DOWNLOADS, ANNOTATOR, TRAINER, REVERSE, FORWARD)
# Algorithm routes, each holding 3_训练器 / 4_逆向决策器 / 5_正向决策器 (4.4.6).
CLASSIC = "常规经典算法"
LLM = "AI大模型算法"
TRACKS = (CLASSIC, LLM)
AREAS = (TRAINER, REVERSE, FORWARD)
TREE_PREFIX = "目录树/"
LEDGER_FOLDER = "台账"
UPLOAD_FOLDER = "上传"
FARM_NAME = "扬大_高邮牧场"
FARM_MARKER = ".cowmata-farm.json"

# Defaults of earlier versions, recognised only to move their data into the tree: a data folder beside
# the app (4.1.2–4.4.5) and the fixed folders of the pre-4.4.1 site.
DATA_DIRECTORY = "COWMATA Pro 数据"
RELATIVE_DATA_ROOT = "../" + DATA_DIRECTORY + "/下载数据"
RELATIVE_LEDGER_ROOT = "../" + DATA_DIRECTORY + "/现场台账"
LEGACY_DATA_ROOTS = (r"F:\牛舍", r"F:\扬大_高邮牧场")
LEGACY_LEDGER_ROOTS = (r"F:\牛舍\_现场记录", r"F:\牛舍_现场记录")

TREE_MISSING = ("找不到目录树（含 1_下载器 的磁盘或文件夹）：请插上存放目录树的磁盘后重试。"
                "数据只保存在目录树里，不会改存到其他位置")


def _drives(kinds=(2, 3)):
    """Local drive roots (removable and fixed by default)."""
    if os.name != "nt":
        return []
    import ctypes
    import string

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(f"{letter}:\\") for i, letter in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") in kinds]


def _system_drive():
    return Path(os.path.splitdrive(os.environ.get("SystemRoot", "C:\\Windows"))[0] + "\\")


def version_key(name):
    """Sort key of a version folder name such as ``4.4.6``; other names sort first."""
    parts = str(name).split(".")
    if not all(p.isdigit() for p in parts):
        return ()
    return tuple(int(p) for p in parts)


def _version_folders(base, depth=3):
    """Version-named folders at most ``depth`` levels below ``base`` (not below a version folder)."""
    try:
        children = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []
    found = [p for p in children if version_key(p.name)]
    if depth > 1:
        for child in children:
            if not version_key(child.name):
                found += _version_folders(child, depth - 1)
    return found


def _areas(tree):
    """Area folders (3_训练器 …) of every route, and of the 4.4.5 layout directly in the tree."""
    return [tree / track / area for track in TRACKS for area in AREAS] + [tree / area for area in AREAS]


def _newest_version(tree):
    bases = [tree / ANNOTATOR, *_areas(tree)]
    return max([(), *(version_key(p.name) for base in bases if base.is_dir() for p in _version_folders(base))])


def _rank(tree, app_anchor):
    present = sum((tree / name).is_dir() for name in (DOWNLOADS, ANNOTATOR, *TRACKS, *AREAS))
    return present, _newest_version(tree), tree == app_anchor, -ord(str(tree)[0].upper())


def tree_root(app_root=APP_ROOT):
    """The site tree (the folder holding 1_下载器, 2_标注器 and the algorithm routes), or None.

    ``COWMATA_SITE_TREE`` overrides discovery: a folder → that tree; ``0`` → only a tree the app runs inside
    (tests, or a computer that must not pick up a removable drive). Otherwise:
    1. the tree this app runs inside (portable package unpacked under ``<tree>\\2_标注器\\<版本>``);
    2. the most complete tree on the local drives (most top folders, newest version folder, the app's own
       drive on ties).
    """
    override = os.environ.get("COWMATA_SITE_TREE", "").strip()
    if override not in ("", "0"):
        tree = Path(override)
        return tree if (tree / DOWNLOADS).is_dir() else None
    app = Path(app_root).resolve()
    for folder in app.parents:
        if (folder / DOWNLOADS).is_dir():
            return folder
    if override == "0":
        return None
    found = [d for d in _drives() if (d / DOWNLOADS).is_dir()]
    if not found:
        return None
    anchor = Path(app.anchor)
    return max(found, key=lambda d: _rank(d, anchor))


def new_tree_root(app_root=APP_ROOT):
    """Where a computer without any tree starts one: the app's drive, or a data drive when the app sits on the
    system drive (never inside the program folder). With ``COWMATA_SITE_TREE=0`` (tests) the tree starts in
    the folder holding the app, so nothing is ever created on a real drive root; with ``COWMATA_SITE_TREE=<folder>``
    the tree starts in that folder."""
    app = Path(app_root).resolve()
    override = os.environ.get("COWMATA_SITE_TREE", "").strip()
    if override == "0":
        return app.parent
    if override:
        return Path(override)
    anchor = Path(app.anchor)
    if anchor != _system_drive():
        return anchor
    others = [d for d in _drives((3,)) if d != anchor]
    return others[0] if others else anchor


def farm_in(tree):
    """<tree>\\1_下载器\\<牧场>: the preferred farm, else a marked farm folder, else the first folder."""
    base = Path(tree) / DOWNLOADS
    preferred = base / FARM_NAME
    if preferred.is_dir():
        return preferred
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        folders = []
    marked = [p for p in folders if (p / FARM_MARKER).is_file()]
    return (marked or folders or [preferred])[0]


def site_farm(app_root=APP_ROOT):
    """<tree>\\1_下载器\\<牧场> of the site tree, or None when no tree is found."""
    tree = tree_root(app_root)
    return farm_in(tree) if tree is not None else None


def site_ledger(app_root=APP_ROOT):
    farm = site_farm(app_root)
    return farm / LEDGER_FOLDER if farm is not None else None


def default_data_root(app_root=APP_ROOT):
    """Download folder of a new setting: the tree's farm; a computer without a tree starts one."""
    return farm_in(tree_root(app_root) or new_tree_root(app_root))


def default_ledger_root(app_root=APP_ROOT):
    return default_data_root(app_root) / LEDGER_FOLDER


def follow_tree(path, app_root=APP_ROOT):
    """A saved absolute location that no longer exists but lay inside a site tree (…\\1_下载器\\… etc.)
    re-anchored on the tree found now — the tree was moved or copied to another drive. None otherwise."""
    path = Path(path)
    tree = tree_root(app_root)
    if tree is None or path.exists():
        return None
    parts = path.parts
    for index, part in enumerate(parts):
        if part in TREE or part in TRACKS:
            candidate = tree.joinpath(*parts[index:])
            return candidate if candidate.exists() or candidate.parent.exists() else None
    return None


def start_folder(app_root=APP_ROOT):
    """Start folder of file dialogs: the tree's farm, else the tree, else the working directory."""
    tree = tree_root(app_root)
    if tree is None:
        return Path.cwd()
    farm = farm_in(tree)
    return farm if farm.is_dir() else tree


def resolve_location(value, app_root=APP_ROOT):
    """Expand a saved location: ``目录树/…`` (inside the site tree), an absolute path, or a path relative to
    the app (data folders beside the app, 4.1.2–4.4.5)."""
    from .core import DownloadError

    if not isinstance(value, str) or not value.strip():
        raise DownloadError("请选择专用数据文件夹")
    text = value.strip()
    if text.replace("\\", "/").startswith(TREE_PREFIX):
        rest = PurePosixPath(text.replace("\\", "/")[len(TREE_PREFIX):])
        if not rest.parts or rest.is_absolute() or ".." in rest.parts or ":" in rest.parts[0]:
            raise DownloadError("目录树内的位置无效：" + text)
        tree = tree_root(app_root)
        if tree is None:
            raise DownloadError(TREE_MISSING)
        resolved = tree.joinpath(*rest.parts).resolve()
    else:
        native = PureWindowsPath(text)
        if (native.drive or native.root) and not native.is_absolute():
            raise DownloadError("路径不能只有盘符或从盘符根开始，请使用完整路径或相对软件的路径")
        path = Path(text)
        # Absolute custom paths on Windows remain supported for existing datasets.
        if path.is_absolute():
            resolved = path.resolve()
            moved = follow_tree(resolved, app_root)
            if moved is not None:
                resolved = moved.resolve()
        else:
            if native.is_absolute():
                raise DownloadError("此系统不能使用 Windows 磁盘路径")
            resolved = (Path(app_root) / path).resolve()
    if resolved == Path(resolved.anchor) or resolved == Path(app_root).resolve().parent:
        raise DownloadError("请选择专用数据文件夹，不能直接使用磁盘或安装位置的父目录")
    if resolved == Path(app_root).resolve() or resolved.is_relative_to(Path(app_root).resolve()):
        raise DownloadError("数据请放在目录树的 1_下载器 里，不要放进软件目录（升级时会被替换）")
    return resolved


def relative_location(value, app_root=APP_ROOT):
    """Saved form of a location: ``目录树/…`` inside the site tree, a path relative to the app inside the
    4.1.2–4.4.5 data folder beside it, otherwise the absolute path (a folder the operator chose elsewhere)."""
    path = resolve_location(str(value), app_root)
    tree = tree_root(app_root)
    if tree is not None and path != tree and path.is_relative_to(tree):
        return TREE_PREFIX + path.relative_to(tree).as_posix()
    managed = (Path(app_root).parent / DATA_DIRECTORY).resolve()
    if path.is_relative_to(managed):
        return Path(os.path.relpath(path, app_root)).as_posix()
    return str(path)


def in_tree(value):
    return isinstance(value, str) and value.replace("\\", "/").startswith(TREE_PREFIX)


def is_legacy(path, legacy):
    return str(PureWindowsPath(str(path))).casefold() in {str(PureWindowsPath(p)).casefold() for p in legacy}
