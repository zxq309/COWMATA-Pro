"""Independent Pro download preferences; reuse tested downloader settings on first use."""

import json
import os
from pathlib import Path, PureWindowsPath

from .core import DownloadError
from .paths import (APP_ROOT, RELATIVE_DATA_ROOT, RELATIVE_LEDGER_ROOT,
                    documents_root, relative_location, resolve_location)
from .settings import SettingsStore, validated
from .site_records import settings_defaults, validate_connection


class ProSettings(SettingsStore):
    def __init__(self, directory=None, *, app_root=None):
        self.app_root = Path(app_root).resolve() if app_root else APP_ROOT
        self.default_data_root = resolve_location(RELATIVE_DATA_ROOT, self.app_root)
        self.default_ledger_root = resolve_location(RELATIVE_LEDGER_ROOT, self.app_root)
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
            **settings_defaults(),
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

    def _repair_legacy_paths(self):
        """Repair only unavailable fixed defaults; preserve custom/removable paths."""
        old = dict(self.value)
        data = Path(self.value["data_root"])
        records = Path(self.value["ledger_directory"])
        legacy_data = {PureWindowsPath(r"F:\牛舍"), PureWindowsPath(r"F:\扬大_高邮牧场")}
        legacy_records = {PureWindowsPath(r"F:\牛舍\_现场记录"), PureWindowsPath(r"F:\牛舍_现场记录")}
        if PureWindowsPath(str(data)) in legacy_data and not Path(data.anchor).is_dir():
            self.value["data_root"] = str(self._previous_data_root())
        if PureWindowsPath(str(records)) in legacy_records and not records.is_dir():
            self.value["ledger_directory"] = str(self.default_ledger_root)
        # Replace generated defaults only when they contain no existing files.
        # Existing datasets/caches retain their location and exact bytes.
        generated = {
            "data_root": {documents_root() / "COWMATA Pro/下载数据"},
            "ledger_directory": {self.directory / "现场台账", documents_root() / "COWMATA Pro/现场台账"},
        }
        for key, candidates in generated.items():
            path = Path(self.value[key])
            if path in candidates and not path.exists():
                self.value[key] = str(self.default_data_root if key == "data_root" else self.default_ledger_root)
        if old != self.value:
            self.notice = "已自动定位数据目录。更新台账后即可下载，原文件保留。"
            if self.path.is_file():
                self._before_path_repair = self.path.read_bytes()

    def _previous_data_root(self):
        """Reuse an existing standalone-downloader root when the old F: drive is gone."""
        path = self._local_root / "COWMATA/AutoDownloader/automatic-download.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            candidate = Path(value.get("data_root", ""))
            if candidate.is_absolute() and candidate != Path(candidate.anchor) and candidate.is_dir():
                return candidate
        except (OSError, ValueError, TypeError):
            pass
        return self.default_data_root

    def decode(self, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for key in ("data_root", "ledger_directory"):
            if key in value:
                value[key] = str(resolve_location(value[key], self.app_root))
        return value

    def encode(self, value):
        value = dict(value)
        for key in ("data_root", "ledger_directory"):
            if key in value:
                value[key] = relative_location(value[key], self.app_root)
        return value

    def display_path(self, value):
        return relative_location(value, self.app_root)

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
        for key in ("data_root", "ledger_directory"):
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
