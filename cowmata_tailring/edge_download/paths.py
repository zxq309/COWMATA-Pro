"""Installation-relative data locations kept outside replaceable program files."""

import os
from pathlib import Path, PureWindowsPath


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
DATA_DIRECTORY = "COWMATA Pro 数据"
RELATIVE_DATA_ROOT = "../" + DATA_DIRECTORY + "/下载数据"
RELATIVE_LEDGER_ROOT = "../" + DATA_DIRECTORY + "/现场台账"


def resolve_location(value, app_root=APP_ROOT):
    """Expand a saved relative location into a dedicated runtime directory."""
    from .core import DownloadError

    if not isinstance(value, str) or not value.strip():
        raise DownloadError("请选择专用数据文件夹")
    native = PureWindowsPath(value)
    if (native.drive or native.root) and not native.is_absolute():
        raise DownloadError("路径不能只有盘符或从盘符根开始，请使用完整路径或相对软件的路径")
    path = Path(value)
    # Absolute custom paths on Windows remain supported for existing datasets.
    if path.is_absolute():
        resolved = path.resolve()
    else:
        if native.is_absolute():
            raise DownloadError("此系统不能使用 Windows 磁盘路径")
        resolved = (Path(app_root) / path).resolve()
    if resolved == Path(resolved.anchor) or resolved == Path(app_root).resolve().parent:
        raise DownloadError("请选择专用数据文件夹，不能直接使用磁盘或安装位置的父目录")
    if resolved == Path(app_root).resolve() or resolved.is_relative_to(Path(app_root).resolve()):
        raise DownloadError("数据请放在软件旁的数据文件夹，避免升级时覆盖；默认位置已自动设置")
    return resolved


def relative_location(value, app_root=APP_ROOT):
    """Serialize managed sibling paths relatively; retain explicit external roots."""
    path = resolve_location(str(value), app_root)
    managed = (Path(app_root).parent / DATA_DIRECTORY).resolve()
    if path.is_relative_to(managed):
        return Path(os.path.relpath(path, app_root)).as_posix()
    return str(path)


DEFAULT_DATA_ROOT = resolve_location(RELATIVE_DATA_ROOT)
DEFAULT_LEDGER_ROOT = resolve_location(RELATIVE_LEDGER_ROOT)

# 4.4.5 site layout: <drive>\1_下载器\<牧场>  (raw data by category + 台账 CSVs),
# <drive>\2_标注器 (this app + 科牧特_协作标注), <drive>\3_训练器, <drive>\4_逆向决策器, <drive>\5_正向决策器.
DOWNLOADS = "1_下载器"
ANNOTATOR = "2_标注器"
LEDGER_FOLDER = "台账"
FARM_NAME = "扬大_高邮牧场"
# Folders of the pre-4.4.1 layout; settings pointing here are moved to the site layout.
LEGACY_DATA_ROOTS = (r"F:\牛舍", r"F:\扬大_高邮牧场")
LEGACY_LEDGER_ROOTS = (r"F:\牛舍\_现场记录", r"F:\牛舍_现场记录")


def _drives():
    if os.name != "nt":
        return []
    import ctypes
    import string

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(f"{letter}:\\") for i, letter in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") in (2, 3)]


def site_farm(app_root=APP_ROOT):
    """<drive>\\1_下载器\\<牧场> of this site: the app's own drive first, then every local drive."""
    anchors = [Path(Path(app_root).anchor)] + [d for d in _drives() if d != Path(Path(app_root).anchor)]
    for anchor in anchors:
        if not anchor.is_dir():
            continue
        base = anchor / DOWNLOADS
        preferred = base / FARM_NAME
        if preferred.is_dir():
            return preferred
        try:
            farms = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        if farms:
            return farms[0]
    return None


def site_ledger(app_root=APP_ROOT):
    farm = site_farm(app_root)
    return farm / LEDGER_FOLDER if farm is not None else None


def is_legacy(path, legacy):
    return str(PureWindowsPath(str(path))).casefold() in {str(PureWindowsPath(p)).casefold() for p in legacy}
