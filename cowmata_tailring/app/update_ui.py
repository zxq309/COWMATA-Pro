"""Nonblocking update UI; installation is handed off only after normal app exit."""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring import __version__
from cowmata_tailring.ui.i18n import get_language

from . import update_core as core
from . import update_worker as worker


def tr(zh, en):
    return en if get_language() == "en" else zh


def prepare_job(root, setup, update, cache):
    root = worker.safe_path(root)
    portable = update.get("kind") == "portable_zip"
    if portable:
        if not worker.is_product_installation(root):
            raise ValueError("请选择带完整文件清单的 COWMATA 程序目录")
        old_version = worker.installation_version(root)
        from .portable_update import zip_plan
        if core.version_key(update["version"]) <= core.version_key(old_version):
            raise ValueError("所选版本必须高于当前软件版本")
        if Path(setup).resolve().is_relative_to(root):
            raise ValueError("请把更新 ZIP 放在目标软件目录之外，再选择更新")
        zip_plan(setup, update["version"])
    else:
        old_version = (root / "COWMATA.install-id").read_text(encoding="utf-8").removeprefix("COWMATA-")
        if not worker.registered(root, old_version):
            raise ValueError("此副本请选择完整便携 ZIP 更新")
    worker.inventory(root)
    job_dir = worker.safe_path(cache / ("job-" + uuid.uuid4().hex))
    job_dir.mkdir()
    # Only the ~30 MB embedded standard library is needed, not Qt, models or
    # system Python. It remains outside the tree that will be renamed.
    runtime = job_dir / "runtime"
    runtime.mkdir()
    runtime_source = Path(__file__).resolve().parents[2] / "runtime"
    if not runtime_source.is_dir():
        runtime_source = root / "runtime"
    for path in runtime_source.iterdir():
        if path.is_file() and path.suffix in {".exe", ".dll", ".zip", ".pyd"}:
            shutil.copy2(path, runtime / path.name)
    zips = list(runtime.glob("python3*.zip"))
    if len(zips) != 1:
        raise ValueError("Private updater runtime is incomplete")
    (runtime / (zips[0].stem + "._pth")).write_text(zips[0].name + "\n.\n..\n", encoding="utf-8")
    for name in ("update_core.py", "update_windows.py", "update_worker.py", "portable_update.py"):
        shutil.copy2(Path(__file__).with_name(name), job_dir / name)
    shutil.copy2(Path(__file__).resolve().parents[2]/'COWMATA.exe', job_dir/'COWMATA-Progress.exe')
    job = {"root": str(root), "setup": str(setup), "update": update, "job_dir": str(job_dir),
           "desktop": False if portable else worker.desktop_enabled(root, old_version),
           "request_close": True}
    worker.write_json(job_dir / "job.json", job)
    result = worker.run([runtime / "python.exe", "-I", "-B", job_dir / "update_worker.py", "--help"], timeout=15)
    if result.returncode:
        raise RuntimeError("Detached updater could not start")
    return job_dir / "job.json"


class UpdateController(QObject):
    found = Signal(object)
    failed = Signal(str)
    downloaded = Signal(object)
    progress = Signal(int, int)
    prepared = Signal(object)
    local_ready = Signal(object)

    def __init__(self, window, *, automatic=True):
        super().__init__(window)
        self.window = window
        self.settings = QSettings()
        self.root = Path(__file__).resolve().parents[2]
        self.cache = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator" / "updates").resolve()
        self._cleanup_completed_update_cache()
        self.update = None
        self.setup = None
        self.pending_job = None
        self.busy = False
        self.stop = threading.Event()
        self.dialog = None
        self.notification = None
        self._announced_packages = set()
        self.status = tr("软件可直接离线启动；更新在后台检查，也可手动检查。", "The app starts offline. Updates are checked in the background or on demand.")
        self.button = QPushButton(tr("检查更新", "Updates"))
        self.button.clicked.connect(self.open_dialog)
        # Update entry lives in Help > About. Keep the controller's status
        # button hidden for compatibility; automatic notifications still work.
        self.button.setParent(window)
        self.button.hide()
        self.found.connect(self._found)
        self.failed.connect(self._failed)
        self.downloaded.connect(self._download_result)
        self.progress.connect(self._progress)
        self.prepared.connect(self._prepare_result)
        self.local_ready.connect(self._local_ready)
        self.timer = QTimer(self)
        self.timer.setInterval(30 * 60 * 1000)
        self.timer.timeout.connect(self.auto_check)
        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self._close_for_update)
        if automatic:
            self.timer.start()
            QTimer.singleShot(1500, self.startup_check)
        QApplication.instance().aboutToQuit.connect(self.on_exit)

    def _cleanup_completed_update_cache(self):
        """Remove only updater-owned artifacts from already completed jobs.

        Project data, labels, models and resumable downloads are deliberately
        outside this cleanup.  A job is eligible only after the detached
        updater has written ``phase=complete``; interrupted or failed jobs are
        retained so a later launch can show a useful diagnostic.
        """
        try:
            cache = self.cache
            if not cache.is_dir():
                return
            for job_dir in cache.glob("job-*"):
                if not job_dir.is_dir() or job_dir.is_symlink():
                    continue
                try:
                    result_path = job_dir / "result.json"
                    state = json.loads(result_path.read_text(encoding="utf-8"))
                    if state.get("phase") != "complete":
                        continue
                    job_path = job_dir / "job.json"
                    job = json.loads(job_path.read_text(encoding="utf-8")) if job_path.is_file() else {}
                    setup = Path(job.get("setup", "")).resolve() if job.get("setup") else None
                    if setup is not None:
                        downloads = (cache / "downloads").resolve()
                        if setup.parent == downloads / setup.parent.name and setup.parent.parent == downloads:
                            for candidate in (setup, Path(str(setup) + ".part")):
                                if candidate.is_file() and not candidate.is_symlink():
                                    candidate.unlink()
                            try:
                                setup.parent.rmdir()
                            except OSError:
                                pass
                    shutil.rmtree(job_dir)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # A running/partially written job is left untouched.
                    continue
        except (OSError, ValueError):
            return

    def option(self, key, default=True):
        return self.settings.value("updates/" + key, default, type=bool)

    def channel(self):
        default = "preview" if core.version_key(__version__)[3] != 3 else "stable"
        return self.settings.value("updates/channel", default, type=str)

    def _task(self, function, signal):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.render()

        def run():
            try:
                if os.name == "nt":
                    import ctypes
                    kernel = ctypes.WinDLL("kernel32")
                    kernel.GetCurrentThread.restype = ctypes.c_void_p
                    kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    # Thread-local background I/O and scheduling; never lower
                    # the UI/video process or alter the global power plan.
                    kernel.SetThreadPriority(kernel.GetCurrentThread(), 0x10000)
                result = function()
                if not self.stop.is_set():
                    signal.emit(result)
                else:
                    self.failed.emit(tr("下载已暂停，可稍后续传。", "Paused. You can resume later."))
            except Exception as exc:
                try:
                    self.failed.emit(str(exc))
                except RuntimeError:  # QObject destroyed during normal shutdown.
                    pass
        threading.Thread(target=run, name="cowmata-update", daemon=True).start()

    def startup_check(self):
        """Each launch checks after the workspace is visible, without a network gate."""
        if self.pending_job or self.stop.is_set():
            return
        if self.busy:
            QTimer.singleShot(1000, self.startup_check)
            return
        self.check()

    def auto_check(self):
        if self.option("auto_check") and not self.busy and not self.pending_job:
            last = self.settings.value('updates/last_auto_check', 0.0, type=float)
            if 0 <= time.time() - last < 1800:
                return
            self.settings.setValue('updates/last_auto_check', time.time())
            self.check()

    def check_release(self, channel=None):
        channel = channel or self.channel()
        if (self.root / "COWMATA.install-id").is_file():
            return core.check_update(__version__, channel, package_kind="installer")
        return core.check_update(__version__, channel)

    def check(self):
        if self.busy:
            return
        self.status = tr("正在检查 GitHub 发布版本…", "Checking GitHub releases…")
        self._task(lambda: self.check_release(), self.found)

    def _found(self, update):
        self.busy = False
        self.pending_job = None
        previous = self.update
        self.update = update
        if self.same_package(update, previous) and self.setup:
            self._downloaded(self.setup)
            return
        self.setup = None
        if update:
            self.status = tr("发现新版本：", "New release: ") + update["version"]
            self.button.setText(tr("发现更新", "Update available"))
            self.button.setStyleSheet("color:#b36b00; font-weight:600")
            self.render()
            self.announce(update)
            if self.option("auto_download"):
                self.start_download()
        else:
            if self.notification is not None:
                self.notification.close()
            self.button.setText(tr("检查更新", "Updates"))
            self.button.setStyleSheet("")
            self.status = tr("当前已是此通道的最新版本。", "This is the newest version in this channel.")
            self.render()

    def announce(self, update):
        identity = update["version"] + ":" + update["sha256"]
        if identity in self._announced_packages:
            return
        if self.notification is not None:
            self.notification.close()
            self.notification.deleteLater()
        self._announced_packages.add(identity)
        self.notification = QMessageBox(self.window)
        self.notification.setWindowTitle(tr("COWMATA Pro™ 有新版本", "COWMATA Pro™ update available"))
        self.notification.setText(tr("发现新版本：", "New version: ") + update["version"] + tr(
            "\n在“帮助 → 关于 → 版本与更新”查看新版下载与进度，可直接升级，无须逐个安装旧版本。不会强制关闭正在标注的工程。",
            "\nOpen Help > About > Version and updates for the latest download and progress. Upgrade directly without installing intermediate releases. Your annotation session stays open."))
        self.notification.setIcon(QMessageBox.Icon.Information)
        details = self.notification.addButton(tr("查看更新", "View update"), QMessageBox.ButtonRole.ActionRole)
        details.clicked.connect(self.open_dialog)
        self.notification.addButton(tr("稍后", "Later"), QMessageBox.ButtonRole.RejectRole)
        self.notification.setWindowModality(Qt.WindowModality.NonModal)
        self.notification.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.notification.show()

    def _failed(self, message):
        self.busy = False
        self.status = tr("更新暂停（标注不受影响）：", "Update paused (annotation is unaffected): ") + message
        self.render()

    def start_download(self):
        if not self.update or self.busy:
            return
        if self.update.get("source") == "local":
            self.select_local_package()
            return
        update = dict(self.update)
        channel = self.channel()
        self.setup = None
        self.pending_job = None
        self.status = tr("后台下载中；不会自动退出标注。", "Downloading in the background; annotation stays open.")
        directory = self.cache / "downloads" / update["sha256"]

        def download_latest():
            latest = self.check_release(channel)
            if not self.same_package(update, latest):
                return channel, latest, None
            path = core.download(latest, directory, self.progress.emit, self.stop.is_set)
            if self.stop.is_set():
                raise InterruptedError("Paused")
            latest = self.check_release(channel)
            return channel, latest, path if self.same_package(update, latest) else None

        self._task(download_latest, self.downloaded)

    @staticmethod
    def same_package(first, second):
        return bool(first and second and first["sha256"] == second["sha256"]
                    and core.version_key(first["version"]) == core.version_key(second["version"]))

    def _download_result(self, result):
        channel, update, path = result
        self.busy = False
        if channel != self.channel():
            self.check()
        elif path is None:
            self._found(update)
        else:
            self.update = update
            self._downloaded(path)

    def _downloaded(self, path):
        self.busy = False
        self.setup = Path(path)
        self.status = tr("下载及 SHA-256 校验完成。点击“保存退出并更新”后安装。", "Downloaded and SHA-256 verified. Choose Save, exit and update to install.")
        self.button.setText(tr("更新已下载", "Update ready"))
        self.render()

    def _progress(self, current, total):
        if self.dialog:
            self.bar.setValue(round(current / max(total, 1) * 1000))
            self.info.setText(tr("后台下载：", "Downloading: ") + f"{current / 1024**2:.1f} / {total / 1024**2:.1f} MB")

    def install(self):
        if not self.setup or self.busy:
            return
        answer = QMessageBox.question(self.window, tr("保存后更新", "Update after saving"),
            tr("将正常保存并关闭所有标注窗口，在原位置更新后重新打开。\n原始数据、标签和已有校准不移动。是否继续？",
               "Save and close all annotation windows, update in place, then reopen?\nSource data, labels and calibration will not be moved."))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._prepare_install()

    def _prepare_install(self):
        update, setup, channel = dict(self.update), self.setup, self.channel()
        self.pending_job = None
        self.status = tr("正在确认最新版并准备独立更新程序…", "Checking the latest release and preparing the independent updater…")

        def prepare_latest():
            latest = update if update.get("source") == "local" else self.check_release(channel)
            job = prepare_job(self.root, setup, latest, self.cache) if self.same_package(update, latest) else None
            return channel, latest, job

        self._task(prepare_latest, self.prepared)

    def _prepare_result(self, result):
        channel, update, job = result
        self.busy = False
        if channel != self.channel():
            self.setup = None
            self.check()
        elif job is None:
            self._found(update)
        else:
            self.update = update
            self._prepared(job)

    def _prepared(self, job):
        self.busy = False
        self.pending_job = Path(job)
        self.status = tr("等待所有窗口成功保存并关闭；未成功保存时不会更新。", "Waiting for every window to save and close. Failed saves prevent installation.")
        self.render()
        if self.dialog:
            self.dialog.close()
        # closeAllWindows honours every closeEvent; quit() would bypass saves.
        QTimer.singleShot(0, self._close_for_update)

    def _close_for_update(self):
        from shiboken6 import isValid
        # Keep existing Python owners before closing. Recreating wrappers from
        # Qt's global native window list while GC destroys closed dialogs can
        # crash in PySide::getWrapperForQObject (Linux/Python 3.10).
        tracked, pending, seen = [], [self.window], set()
        while pending:
            window = pending.pop()
            if id(window) in seen or not isValid(window):
                continue
            seen.add(id(window))
            tracked.append(window)
            pending.extend(getattr(window, '_task_windows', {}).values())
            pending.extend(child for child in window.findChildren(QWidget) if child.isWindow())
        QApplication.closeAllWindows()
        visible = [w for w in tracked
                   if isinstance(w, QWidget) and isValid(w) and w.isVisible()]
        if any(getattr(w,'_closing_requested',False) or getattr(w,'_closing_due_to_organization',False)
               or getattr(w,'_export_running',False) for w in visible):
            self.close_timer.start(200)
            return
        if visible:
            # A cancelled save/close must not leave an old install queued for
            # an unrelated exit hours later. The next attempt rechecks latest.
            self.pending_job = None
            self.status = tr("已取消退出更新，可继续标注；下次更新时将重新检查最新版。",
                             "Update exit cancelled. Continue annotating; the next attempt checks the latest release again.")
            self.render()

    def on_exit(self):
        self.stop.set()
        self.timer.stop()
        self.close_timer.stop()
        if not self.pending_job:
            return
        job_dir = self.pending_job.parent
        try:
            subprocess.Popen([str(job_dir / "runtime/pythonw.exe"), "-I", "-B",
                              str(job_dir / "update_worker.py"), str(self.pending_job)],
                             cwd=job_dir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            worker.write_json(job_dir / "error.json", {"error": str(exc)})
            QMessageBox.critical(self.window, "COWMATA Pro™", str(exc))

    def clear_download(self):
        if self.busy or not self.update or self.pending_job:
            return
        answer = QMessageBox.question(self.window, tr("清除下载缓存", "Clear download"),
            tr("仅删除此版本的更新包与未完成下载，不删除标注数据。", "Remove only this version's installer/partial download, never annotations."))
        if answer != QMessageBox.StandardButton.Yes:
            return
        directory = worker.safe_path(self.cache / "downloads" / self.update["sha256"])
        for name in (self.update["name"], self.update["name"] + ".part"):
            path = worker.member(directory, name)
            path.unlink(missing_ok=True)
        self.setup = None
        self.status = tr("已清除本版本下载缓存，可重新下载。", "Download cache cleared. Ready to retry.")
        self.render()

    def select_local_package(self):
        if self.busy:
            return
        name, _ = QFileDialog.getOpenFileName(self.window, "选择完整便携更新包（放在程序目录外）", "", "COWMATA Portable (*.zip)")
        if not name:
            return
        def inspect():
            from .portable_update import local_update
            update = local_update(name)
            if core.version_key(update["version"]) <= core.version_key(__version__):
                raise ValueError("所选版本必须高于当前软件版本")
            return update, name
        self.status = "正在校验本地便携包…"
        self._task(inspect, self.local_ready)

    def _local_ready(self, result):
        self.update, name = result
        self.pending_job = None
        self._downloaded(name)

    def open_dialog(self):
        if not self.dialog:
            self.dialog = QDialog(self.window)
            self.dialog.setWindowTitle(tr("版本与更新", "Version and updates"))
            self.dialog.resize(620, 460)
            box = QVBoxLayout(self.dialog)
            box.addWidget(QLabel("COWMATA Pro™ " + __version__))
            self.info = QLabel()
            self.info.setWordWrap(True)
            box.addWidget(self.info)
            self.bar = QProgressBar()
            self.bar.setRange(0, 1000)
            box.addWidget(self.bar)
            self.notes = QTextBrowser()
            box.addWidget(self.notes, 1)
            options = QHBoxLayout()
            for key, title in (("auto_check", tr("自动检查（30 分钟）", "Check every 30 minutes")),
                               ("auto_download", tr("后台自动下载", "Download automatically"))):
                check = QCheckBox(title)
                check.setChecked(self.option(key))
                check.toggled.connect(lambda value, k=key: self.settings.setValue("updates/" + k, value))
                options.addWidget(check)
            channels = QComboBox()
            channels.addItem(tr("稳定版", "Stable"), "stable")
            channels.addItem(tr("含预览版", "Preview"), "preview")
            channels.setCurrentIndex(1 if self.channel() == "preview" else 0)
            channels.currentIndexChanged.connect(lambda _: self.change_channel(channels.currentData()))
            options.addWidget(channels)
            box.addLayout(options)
            buttons = QHBoxLayout()
            self.actions = []
            for title, callback in (
                (tr("检查", "Check"), self.check), (tr("下载 / 续传", "Download / resume"), self.start_download),
                (tr("暂停", "Pause"), self.stop.set), (tr("清除下载", "Clear"), self.clear_download),
                (tr("保存退出并更新", "Save, exit and update"), self.install),
            ):
                button = QPushButton(title)
                button.clicked.connect(callback)
                buttons.addWidget(button)
                self.actions.append(button)
            box.addLayout(buttons)
            self.local_button = QPushButton("选择本地便携 ZIP 更新…")
            self.local_button.clicked.connect(self.select_local_package)
            box.addWidget(self.local_button)
            release = QPushButton(tr("查看 GitHub 更新日志", "Release notes on GitHub"))
            release.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(core.PAGE + "/latest")))
            box.addWidget(release)
        self.render()
        self.dialog.show()

    def change_channel(self, value):
        if self.busy:
            self.status = tr("当前任务完成后按新通道检查；不会中断下载。", "The new channel applies to the next check.")
        self.settings.setValue("updates/channel", value)

    def render(self):
        self.button.setToolTip(self.status)
        if not self.dialog:
            return
        self.info.setText(self.status)
        self.local_button.setEnabled(not self.busy)
        self.notes.setPlainText(self.update.get("notes", "") if self.update else tr(
            "自动检查官方发布；API 限流时使用官方发布订阅和附件页。完整便携 ZIP 可直接原位置更新，也可选择本地 ZIP。"
            "\n已有安装版通过 ZIP 更新后转为便携版，原位置及已有快捷方式保留。",
            "Updates use official release pages if the API is limited. Complete portable ZIPs support in-place and local updates."
            "\nZIP updates convert installed copies to portable copies and preserve the path and existing shortcuts."))
        enabled = [not self.busy, bool(self.update) and not self.busy,
                   self.busy, bool(self.update) and not self.busy and not self.pending_job,
                   bool(self.setup) and not self.busy]
        for button, value in zip(self.actions, enabled):
            button.setEnabled(value)


class StartupUpdateController(UpdateController):
    """Offer automatic updates with an explicit path into the local client."""

    def __init__(self, window):
        super().__init__(window, automatic=False)
        self.status = tr("启动前正在检查最新版，请稍候…", "Checking the latest release before startup…")

    def _found(self, update):
        self.busy = False
        self.update, self.setup, self.pending_job = update, None, None
        if update is None:
            self.window.accept()
            return
        self.window.setWindowTitle(tr("发现软件更新，可稍后安装", "Update available; installation can wait"))
        self.window.version.setText(__version__ + " → " + update["version"])
        # Startup checks/downloads are mandatory, independent of optional
        # background reminders, saved preferences or previously dismissed UI.
        self.start_download()

    def _downloaded(self, path):
        self.busy = False
        self.setup = Path(path)
        self._prepare_install()

    def _prepared(self, job):
        self.busy = False
        self.pending_job = Path(job)
        self.window.reject()  # main exits; only then may the worker swap files.

    def _failed(self, message):
        self.busy = False
        self.pending_job = None
        self.status = tr("更新暂未完成，可点击“进入软件”继续使用，或重试更新。\n原因：", "Update incomplete. Open the application to continue working, or retry.\nReason: ") + message
        self.render()

    def _progress(self, current, total):
        self.window.bar.setValue(round(current / max(total, 1) * 1000))
        self.status = tr("正在自动下载最新版：", "Downloading the latest release: ") + f"{current / 1024**2:.1f} / {total / 1024**2:.1f} MB"
        self.render()

    def render(self):
        self.window.info.setText(self.status)
        self.window.retry.setEnabled(not self.busy)
        self.window.folder.setEnabled(self.setup is not None and not self.busy)


class StartupUpdateDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("COWMATA Pro™ 启动更新检查", "COWMATA Pro™ startup update check"))
        self.setMinimumWidth(530)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        box = QVBoxLayout(self)
        self.version = QLabel("COWMATA Pro™ " + __version__)
        box.addWidget(self.version)
        instruction = QLabel(tr("发现更新将下载、校验并安装，完成后重新打开软件。\n网络异常或暂不更新，可点击“进入软件”继续使用，无须逐版升级。",
                                "Updates download, verify and install, then reopen the app.\nOpen the application to work offline or update later; intermediate releases are skipped."))
        instruction.setWordWrap(True)
        box.addWidget(instruction)
        self.info = QLabel()
        self.info.setWordWrap(True)
        box.addWidget(self.info)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        box.addWidget(self.bar)
        buttons = QHBoxLayout()
        self.retry = QPushButton(tr("重试更新", "Retry update"))
        self.folder = QPushButton(tr("打开安装包目录", "Open installer folder"))
        self.continue_button = QPushButton(tr("进入软件（稍后更新）", "Open application (update later)"))
        leave = QPushButton(tr("退出软件", "Exit application"))
        for button in (self.retry, self.folder, self.continue_button, leave):
            buttons.addWidget(button)
        box.addLayout(buttons)
        self.updater = StartupUpdateController(self)
        self.retry.clicked.connect(self.updater.check)
        self.folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.updater.setup.parent))) if self.updater.setup else None)
        leave.clicked.connect(self.reject)
        self.continue_button.clicked.connect(self.continue_local)
        self.updater.render()
        QTimer.singleShot(0, self.updater.check)

    def continue_local(self):
        self.updater.stop.set()
        self.updater.pending_job = None
        self.accept()


def verify_startup_update():
    if os.environ.pop('COWMATA_POST_UPDATE_VERSION', '') == __version__:
        return True
    dialog = StartupUpdateDialog()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    # This gate runs before app.exec() and before constructing/opening any
    # project window. Reject exits main; it is never a path into annotation.
    QApplication.instance().aboutToQuit.disconnect(dialog.updater.on_exit)
    dialog.updater.on_exit()
    dialog.updater.pending_job = None
    return accepted
