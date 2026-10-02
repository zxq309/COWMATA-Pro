"""Independent Pro download preferences; reuse tested downloader settings on first use.

4.4.6: data and 台账 live in the site tree (``<目录树>\\1_下载器\\<牧场>`` and its ``台账``) and are saved
relative to it (``目录树/1_下载器/<牧场>``): a tree moved to another drive or computer is followed with no
setting change, and nothing is written outside the tree — a saved tree location whose tree is not plugged in
stops downloading instead of writing somewhere else. Defaults of earlier versions (the data folder beside
the app, Documents, the pre-4.4.1 F: folders) switch to the tree; files still in those folders are moved
into the tree at the start of the next download round (``adopt``).
"""

import json
import os
from pathlib import Path, PurePosixPath

from .core import DownloadError
from .paths import (APP_ROOT, LEDGER_FOLDER, LEGACY_DATA_ROOTS, LEGACY_LEDGER_ROOTS, RELATIVE_DATA_ROOT,
                    RELATIVE_LEDGER_ROOT, TREE_MISSING, TREE_PREFIX, default_data_root, documents_root, in_tree,
                    is_legacy, new_tree_root, relative_location, resolve_location, tree_root)
from .settings import SettingsStore, validated
from .site_records import SERVER_DIRECTORY, settings_defaults, validate_connection

LOCATIONS = ("data_root", "ledger_directory")


class ProSettings(SettingsStore):
    def __init__(self, directory=None, *, app_root=None):
        self.app_root = Path(app_root).resolve() if app_root else APP_ROOT
        self.default_data_root = default_data_root(self.app_root)
        self.default_ledger_root = self.default_data_root / LEDGER_FOLDER
        # Saved 目录树/… locations whose tree is not plugged in: {key: (stand-in path, saved text)}.
        self._unresolved = {}
        # (earlier default folder, tree folder) pairs still holding files; moved by the next download round.
        self.adopt = []
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        directory = Path(directory) if directory else local / "COWMATA/EdgeDownloadPro"
        self._local_root = local
        super().__init__(directory, self.default_data_root)
        self._before_path_repair = None
        if not self.path.exists():
            tested = local / "COWMATA/AutoDownloader/automatic-download.json"
            if tested.is_file():
                try:
                    original = validated(json.loads(tested.read_text(encoding="utf-8")))
                    for key in ("server", "data_root", "start_time", "interval_seconds"):
                        self.value[key] = original[key]
                except (OSError, ValueError, KeyError, TypeError):
                    self.notice = "独立下载器配置未能读取，请检查当前保存目录和连接设置。"
        self.value = {
            **settings_defaults(self.app_root),
            "ledger_directory": str(self.default_ledger_root),
            "raw_connection": "authorized_task",
            "raw_user": "administrator",
            "raw_key": "",
            "raw_remote_port": 8031,
            **self.value,
        }
        self.value["start_on_login"] = False
        if self.value.get("schema_390") != 1:
            self.value.update(
                schema_390=1,
                server="http://device.cowmata.com:8010",
                raw_connection="http",
                kinds=["motion", "pulse", "temp"],
                start_time="2026-08-01T00:00:00+08:00",
            )
        self.value["schema_391"] = 1
        if self.value.get("download_rules_391") != 1:
            self.value.update(
                download_rules_391=1,
                download_mode="",
                auto_enabled=False,
                kinds=["motion", "pulse"],
                next_run=None,
            )
        self.value.update(download_rules_395=1, kinds=["motion", "pulse", "temp"])
        self._repair_legacy_paths()
        if not self.value.get("download_mode"):
            self.value["download_mode"] = "manual"
        self.value.setdefault("scheduled_time", "")
        self.value.setdefault("end_time", "")
        # Forward deciders started with 下载 on this computer (4.4.6): one route per computer is typical.
        if not isinstance(self.value.get("forward_tracks"), list):
            self.value["forward_tracks"] = ["常规经典算法"]

    @property
    def tree_missing(self):
        """A saved 目录树/… location could not be found: downloading is refused until the tree is back."""
        return bool(self._unresolved)

    def _earlier_defaults(self):
        """Default folders of earlier versions, per setting: beside the app (4.1.2–4.4.5) and in Documents."""
        return {
            "data_root": {resolve_location(RELATIVE_DATA_ROOT, self.app_root),
                          documents_root() / "COWMATA Pro/下载数据"},
            "ledger_directory": {resolve_location(RELATIVE_LEDGER_ROOT, self.app_root), self.directory / "现场台账",
                                 documents_root() / "COWMATA Pro/现场台账"},
        }

    def _repair_legacy_paths(self):
        """Switch earlier default and pre-4.4.1 locations to the site tree; keep folders the operator chose."""
        if self._unresolved:
            return
        old = dict(self.value)
        earlier = self._earlier_defaults()
        legacy = {"data_root": LEGACY_DATA_ROOTS, "ledger_directory": LEGACY_LEDGER_ROOTS}
        target = {"data_root": self.default_data_root, "ledger_directory": self.default_ledger_root}
        for key in LOCATIONS:
            path = Path(self.value[key])
            if (is_legacy(path, legacy[key]) and not path.is_dir()) or path in earlier[key]:
                self.value[key] = str(target[key])
        if is_legacy(self.value.get("ledger_server_directory", ""), LEGACY_LEDGER_ROOTS):
            self.value["ledger_server_directory"] = SERVER_DIRECTORY
        # Files an earlier version saved in its default folders move into the tree with the next round.
        tree = tree_root(self.app_root)
        for key in LOCATIONS:
            current = Path(self.value[key])
            if tree is None or not current.is_relative_to(tree):
                continue
            for folder in sorted(earlier[key]):
                try:
                    holds_files = folder.is_dir() and folder != current and any(folder.iterdir())
                except OSError:
                    holds_files = False
                if holds_files:
                    self.adopt.append((folder, current))
        if old != self.value:
            self.notice = (f"下载数据与台账已改到目录树：{self.value['data_root']}"
                           + ("；原保存位置的文件在下一轮下载开始时移入目录树" if self.adopt else ""))
            if self.path.is_file():
                self._before_path_repair = self.path.read_bytes()

    def check_tree(self):
        """Before every download or 台账 round: a saved 目录树/… location must be found — never write elsewhere."""
        if not self._unresolved:
            return
        if tree_root(self.app_root) is None:
            raise DownloadError(TREE_MISSING)
        for key, (stand_in, saved) in list(self._unresolved.items()):
            if str(self.value.get(key)) == stand_in:
                self.value[key] = str(resolve_location(saved, self.app_root))
            del self._unresolved[key]

    def decode(self, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for key in LOCATIONS:
            if key not in value:
                continue
            saved = value[key]
            try:
                value[key] = str(resolve_location(saved, self.app_root))
            except DownloadError:
                if not (in_tree(saved) and tree_root(self.app_root) is None):
                    raise
                # The tree is not plugged in: keep the saved location and refuse to download (check_tree).
                rest = PurePosixPath(saved.replace("\\", "/")[len(TREE_PREFIX):]).parts
                stand_in = str(new_tree_root(self.app_root).joinpath(*rest))
                self._unresolved[key] = (stand_in, saved)
                value[key] = stand_in
        return value

    def encode(self, value):
        value = dict(value)
        for key in LOCATIONS:
            if key not in value:
                continue
            missing = self._unresolved.get(key)
            if missing and str(value[key]) == missing[0]:
                value[key] = missing[1]
            else:
                value[key] = relative_location(value[key], self.app_root)
        return value

    def display_path(self, value):
        """Folder shown in the settings: the real folder; a saved tree location while its tree is missing."""
        for stand_in, saved in self._unresolved.values():
            if str(value) == stand_in:
                return saved
        return str(resolve_location(str(value), self.app_root))

    def resolve_path(self, value):
        return resolve_location(value, self.app_root)

    def save(self, **changes):
        value = self.decode({**self.value, **changes})
        changes = {key: value[key] for key in changes}
        validate_connection(value)
        if value.get("download_mode", "") not in ("", "manual", "automatic", "scheduled"):
            raise DownloadError("请选择手动、自动或定时下载")
        # Login credentials and sessions must never reach preferences on disk.
        if any(k in changes for k in ("session_token", "password", "ledger_password")):
            raise DownloadError("登录凭据不能保存到下载设置")
        for key in LOCATIONS:
            p = Path(value[key])
            if not p.is_absolute() or p == Path(p.anchor):
                raise DownloadError("请选择独立的数据与台账文件夹")
        if Path(value["data_root"]).resolve() == Path(value["ledger_directory"]).resolve():
            raise DownloadError("九轴数据根目录和现场记录目录请分别设置")
        if value.get("raw_connection") not in ("authorized_task", "direct_ssh", "http"):
            raise DownloadError("九轴连接方式无效")
        if type(value.get("sync_ledger")) is not bool:
            raise DownloadError("现场记录同步开关无效")
        if self._before_path_repair is not None:
            backup = self.path.with_name("automatic-download.before-4.1.2.json")
            try:
                with backup.open("xb") as stream:
                    stream.write(self._before_path_repair)
            except FileExistsError:
                pass
        result = super().save(**changes)
        self._before_path_repair = None
        return result


def configured_data_root():
    return Path(ProSettings().value['data_root'])
