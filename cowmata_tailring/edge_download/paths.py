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
LEGACY_DATA_ROOT = Path(r"F:\牛舍")
