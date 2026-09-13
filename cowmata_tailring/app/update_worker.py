"""Detached, manifest-scoped Windows upgrade; never runs from the install tree.

The old directory is retained until extraction, hashes and the private Python
import smoke test pass. A failed directory swap/registration restores the old
directory. Unknown files and redirected paths stop the upgrade before changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISDIR, S_ISLNK

try:
    from .update_core import digest, version_key
except ImportError:  # Copied next to the detached private interpreter.
    from update_core import digest, version_key

REG_BASE = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
LOCK = "COWMATA.update-lock"


class UpdateInProgress(RuntimeError):
    """Another updater already owns this installation transaction."""


@contextmanager
def installation_lock(root):
    """Serialize detached updaters; ownership survives directory renames."""
    key = hashlib.sha256(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()
    busy = "此安装目录已有更新正在进行，请等待其完成；完成后会自动打开软件。"
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, False, "Global\\COWMATA.Update." + key)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            if status == 0x102:  # WAIT_TIMEOUT: do not wait behind an obsolete job.
                raise UpdateInProgress(busy)
            if status not in (0, 0x80):  # Normal or abandoned ownership.
                raise ctypes.WinError(ctypes.get_last_error())
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        # CI exercises the same transaction contract on POSIX. A stable lock
        # inode must not be unlinked while another process may be opening it.
        import fcntl
        import tempfile
        path = Path(tempfile.gettempdir()) / ("cowmata-update-" + key + ".lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateInProgress(busy) from exc
            yield
        finally:
            os.close(descriptor)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_path(path, *, checked_dirs=None):
    path = Path(os.path.abspath(path))
    if path.parent == path or len(path.parts) < 3:
        raise ValueError("Refusing a broad installation target")
    for index, parent in enumerate([path, *path.parents]):
        # A cache lives for ONE inventory pass only. Still lstat every direct
        # entry; never reuse checks across extraction, swap or cleanup passes.
        if index and checked_dirs is not None and parent in checked_dirs:
            break
        try:
            stat = parent.lstat()
        except FileNotFoundError:
            continue
        if S_ISLNK(stat.st_mode) or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked/redirected update path: " + str(parent))
        if checked_dirs is not None and S_ISDIR(stat.st_mode):
            checked_dirs.add(parent)
    return path


def member(root, relative, *, checked_dirs=None):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Unsafe package member")
    p = Path(relative)
    if p.is_absolute() or any(part in {"..", "."} for part in relative.split("/")) or not relative:
        raise ValueError("Unsafe package member")
    result = safe_path(root / p, checked_dirs=checked_dirs)
    if not result.is_relative_to(root) or result == root:
        raise ValueError("Package member escapes installation")
    return result


def inventory(root, *, verify=False, manifest_sha=None, progress=lambda *_: None):
    checked_dirs = set()
    root = safe_path(root, checked_dirs=checked_dirs)
    manifest_path = root / "package-manifest.json"
    if manifest_sha and digest(manifest_path) != manifest_sha:
        raise ValueError("Extracted package manifest checksum mismatch")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    owned = {"package-manifest.json", "COWMATA.install-id", "Uninstall.exe", LOCK, "logs/annotator.log"}
    py_sources = set()
    for number, row in enumerate(data["files"], 1):
        p = member(root, row["path"], checked_dirs=checked_dirs)
        if row["path"] in owned:
            raise ValueError("Duplicate/reserved package member")
        owned.add(row["path"])
        if p.suffix == ".py":
            py_sources.add((p.parent.relative_to(root).as_posix(), p.stem))
        if verify and (not p.is_file() or p.stat().st_size != row["size"] or digest(p) != row["sha256"]):
            raise ValueError("Extracted file checksum mismatch: " + row["path"])
        if verify:
            progress(number, len(data['files']), row['path'])
    actual = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            safe_path(Path(folder) / name, checked_dirs=checked_dirs)
        for name in files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            cache = path.parent.name == "__pycache__" and path.suffix == ".pyc"
            parent = path.parent.parent.relative_to(root).as_posix() if cache else ""
            if rel not in owned and not (cache and (parent, name.split(".", 1)[0]) in py_sources):
                raise ValueError("安装目录内有非软件文件，请先移到数据工程（不会删除）：" + rel)
            actual.append(rel)
    return actual


def run(command, timeout=240):
    command = [str(x) for x in command]
    # NSIS /D= is special: it must be LAST and its path must NOT be quoted,
    # including when the directory contains spaces/Chinese characters.
    if os.name == "nt" and command[-1].startswith("/D="):
        if any(c in command[-1] for c in '"\r\n'):
            raise ValueError("Invalid installer destination")
        command = subprocess.list2cmdline(command[:-1]) + " " + command[-1]
    return subprocess.run(command, timeout=timeout, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def registered(root, version):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_BASE + "COWMATA-" + version) as key:
            return Path(winreg.QueryValueEx(key, "InstallLocation")[0]).resolve() == root.resolve()
    except FileNotFoundError:
        return False


def remove_registration(root, version):
    import winreg
    if not registered(root, version):
        return
    # Only exact version-specific product links created by our installer.
    # Shell folders may be redirected to OneDrive or another drive.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        desktop = Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0]))
        programs = Path(os.path.expandvars(winreg.QueryValueEx(key, "Programs")[0]))
    links = [desktop / f"COWMATA Annotator {version}.lnk",
             programs / f"COWMATA Annotator {version}" / "COWMATA Annotator.lnk",
             programs / f"COWMATA Annotator {version}" / "Uninstall.lnk"]
    for link in links:
        # Names are reserved installer-owned links, but never follow symlinks.
        if link.exists() and not link.is_symlink():
            link.unlink()
    try:
        links[-1].parent.rmdir()
    except OSError:
        pass
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_BASE + "COWMATA-" + version)


def desktop_enabled(root, version):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_BASE + "COWMATA-" + version) as key:
        try:
            return bool(winreg.QueryValueEx(key, "DesktopShortcut")[0])
        except FileNotFoundError:
            # First bootstrap versions defaulted to a desktop shortcut.
            return True


def remove_owned(root):
    """No recursive delete: validate the complete inventory before first unlink."""
    root = safe_path(root)
    actual = inventory(root)
    checked_dirs = set()
    for rel in actual:
        member(root, rel, checked_dirs=checked_dirs).unlink()
    directories = [Path(folder) for folder, _, _ in os.walk(root, topdown=False)]
    for directory in directories:
        directory.rmdir()  # Concurrent/unrecognized content is preserved.


def start_updated_app(root, version):
    environment = {**os.environ, 'COWMATA_POST_UPDATE_VERSION': version}
    return subprocess.Popen([str(root/'COWMATA.exe')], cwd=root, env=environment,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def install(job, *, runner=run, registration=registered, unregister=remove_registration, restart=True):
    with installation_lock(safe_path(job["root"])):
        return _install_locked(job, runner=runner, registration=registration,
                               unregister=unregister, restart=restart)


def _install_locked(job, *, runner, registration, unregister, restart):
    started = time.monotonic()
    root = safe_path(job["root"])
    setup = safe_path(job["setup"])
    update = job["update"]
    version = update["version"]
    version_key(version)
    identity = (root / "COWMATA.install-id").read_text(encoding="utf-8")
    if not identity.startswith("COWMATA-"):
        raise ValueError("Not a managed COWMATA installation")
    old_version = identity.removeprefix("COWMATA-")
    if version_key(version) <= version_key(old_version) or not registration(root, old_version):
        raise ValueError("Refusing downgrade or unregistered installation")
    if setup.stat().st_size != update["size"] or digest(setup) != update["sha256"]:
        raise ValueError("Installer changed after download")
    inventory(root)
    if shutil.disk_usage(root.parent).free < update["unpacked_size"] + 128 * 1024**2:
        raise OSError("磁盘空间不足；旧版保持不变")
    job_dir = safe_path(job["job_dir"])
    if job_dir.is_relative_to(root):
        raise ValueError("Updater must be outside the application directory")
    token = uuid.uuid4().hex[:8]
    stage = safe_path(root.parent / (".cma-" + token + "-stage"))
    backup = safe_path(root.parent / (".cma-" + token + "-backup"))
    if stage.exists() or backup.exists() or len(str(stage)) > 100:
        raise ValueError("No safe staging directory available")
    state = {"root": str(root), "stage": str(stage), "backup": str(backup), "version": version,
             "old_version": old_version, "phase": "waiting", "timings_seconds": {}}
    journal = job_dir / "result.json"

    def phase(value):
        state["phase"] = value
        state["timings_seconds"][value] = round(time.monotonic() - started, 3)
        write_json(journal, state)

    # The client requests closure through its normal save/close handlers. Do
    # not kill it, another annotation window, or another user's application.
    phase('waiting')
    deadline = time.monotonic() + 180
    while runner([root / "COWMATA.exe", "--check-running"], timeout=15).returncode != 0:
        if time.monotonic() > deadline:
            raise TimeoutError("软件仍在运行；取消升级，旧版未修改")
        time.sleep(.5)
    phase("extracting")
    (root / LOCK).write_text(str(os.getpid()), encoding="ascii")
    swapped = False
    try:
        result = runner([setup, "/S", "/STAGE=1", "/D=" + str(stage)], timeout=600)
        if result.returncode:
            raise RuntimeError(f"解包失败 ({result.returncode})，旧版未修改")
        phase("verifying")
        last_progress = [0.0]
        def progress(current, total, path):
            now = time.monotonic()
            if now-last_progress[0] >= .4 or current == total:
                state.update(verified_files=current, total_files=total, current_file=path)
                write_json(journal, state)
                last_progress[0] = now
        inventory(stage, verify=True, manifest_sha=update["package_sha256"], progress=progress)
        if (stage / "COWMATA.install-id").read_text(encoding="utf-8") != "COWMATA-" + version:
            raise ValueError("Staged installer version does not match")
        phase("import_testing")
        # Import the actual private Qt/workspace/updater stack before swapping.
        smoke = ("import sys;sys.path.insert(0,sys.argv[1]);"
                 "from cowmata_tailring import __version__;"
                 "from cowmata_tailring.workspace.modern_window import MainWindow;"
                 "from cowmata_tailring.app.update_ui import UpdateController;"
                 "print(__version__)")
        result = runner([stage / "runtime/python.exe", "-I", "-B", "-c", smoke, stage], timeout=90)
        output = result.stdout.decode("utf-8", "replace").strip()
        if result.returncode or version_key(output) != version_key(version):
            raise RuntimeError("新版运行库导入检查失败；旧版未修改")
        phase("pre_swap_check")
        inventory(root)  # Catch files added during extraction.
        if runner([root / "COWMATA.exe", "--check-running"], timeout=15).returncode:
            raise RuntimeError("软件被重新打开；已取消替换")
        (stage / LOCK).write_text(str(os.getpid()), encoding="ascii")
        phase("swapping")
        root.rename(backup)
        try:
            stage.rename(root)
        except OSError:
            backup.rename(root)
            raise
        swapped = True
        phase("registering")
        result = runner([setup, "/S", "/REGISTERONLY=1",
                         "/DESKTOP=" + ("1" if job.get("desktop", True) else "0"), "/D=" + str(root)])
        if result.returncode:
            raise RuntimeError(f"新版注册失败 ({result.returncode})")
        phase("committed")
    except Exception as exc:
        if swapped:
            # No new application is started before commit.
            try:
                unregister(root, version)
            except (OSError, ValueError) as cleanup_error:
                # A locked shortcut/registry key must not prevent restoring
                # the previous application directory after registration fails.
                state["rollback_warning"] = str(cleanup_error)
            root.rename(stage)
            backup.rename(root)
        (root / LOCK).unlink(missing_ok=True)
        state["error"] = str(exc)
        phase("rolled_back" if swapped else "failed_before_swap")
        # Keep failed staging as diagnostic evidence, never erase unknown files.
        raise
    (root / LOCK).unlink(missing_ok=True)
    if restart:
        phase('restarting')
        try:
            start_updated_app(root, version)
            state['application_launched'] = True
        except OSError as exc:
            state['restart_warning'] = str(exc)
    try:
        phase("cleaning_backup")
        unregister(root, old_version)
        remove_owned(backup)
    except (OSError, ValueError) as exc:
        state["cleanup_warning"] = str(exc)
    (root / LOCK).unlink(missing_ok=True)
    phase("complete")
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    args = ap.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    write_json(Path(job['job_dir'])/'result.json', {'phase':'preparing'})
    progress_exe = Path(job['job_dir'])/'COWMATA-Progress.exe'
    if os.name == 'nt' and progress_exe.is_file():
        subprocess.Popen([str(progress_exe),'--update-progress',str(Path(job['job_dir'])/'result.json'),str(os.getpid())],
                         cwd=job['job_dir'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        install(job)
        return 0
    except UpdateInProgress as exc:
        write_json(Path(job["job_dir"]) / "result.json", {"phase": "already_updating", "message": str(exc)})
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "COWMATA Annotator 更新", 0x40)
        return 0
    except Exception as exc:
        write_json(Path(job["job_dir"]) / "error.json", {"error": str(exc)})
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "升级未完成，未强行覆盖。请查看：\n" +
                str(Path(job["job_dir"]) / "error.json") + "\n\n" + str(exc), "COWMATA Annotator 更新", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
