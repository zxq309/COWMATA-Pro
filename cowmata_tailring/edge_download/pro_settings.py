"""Independent Pro download preferences; reuse tested downloader settings on first use."""

import json
import os
from pathlib import Path

from .core import DownloadError
from .paths import DEFAULT_DATA_ROOT, LEGACY_DATA_ROOT
from .settings import SettingsStore, validated
from .site_records import (
    LOCAL_DIRECTORY,
    SCHEMAS,
    SERVER_DIRECTORY,
    settings_defaults,
    validate_connection,
)


class ProSettings(SettingsStore):
    def __init__(self, directory=None):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        directory = Path(directory) if directory else local / "COWMATA/EdgeDownloadPro"
        super().__init__(directory, DEFAULT_DATA_ROOT)
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
            "raw_connection": "authorized_task",
            "raw_user": "administrator",
            "raw_key": "",
            "raw_remote_port": 8031,
            **self.value,
        }
        self.value['start_on_login'] = False
        if self.value.get('schema_390') != 1:
            self.value.update(schema_390=1,
                              server='http://device.cowmata.com:8010', raw_connection='http',
                              kinds=['motion','pulse','temp'], start_time='2026-08-01T00:00:00+08:00')
        if self.value.get('schema_391') != 1:
            if Path(self.value['data_root']) == LEGACY_DATA_ROOT:
                self.value['data_root'] = str(DEFAULT_DATA_ROOT)
                self.notice = '已修正 3.9 的默认下载位置；旧目录文件保留，请核对后合并。'
            self.value['schema_391'] = 1
        if self.value.get('download_rules_391') != 1:
            self.value.update(download_rules_391=1, download_mode='', auto_enabled=False,
                              kinds=['motion', 'pulse'], next_run=None)
        legacy_records = Path(LOCAL_DIRECTORY)
        existing_records = Path(SERVER_DIRECTORY)
        if (Path(self.value["ledger_directory"]) == legacy_records
                and not legacy_records.exists()
                and all((existing_records / schema["filename"]).is_file() for schema in SCHEMAS.values())):
            self.value["ledger_directory"] = str(existing_records)
        self.value.setdefault('scheduled_time', '')
        self.value.setdefault('end_time', '')

    def save(self, **changes):
        value = {**self.value, **changes}
        validate_connection(value)
        if value.get("download_mode", "") not in ("", "manual", "automatic", "scheduled"):
            raise DownloadError("请选择手动、自动或定时下载")
        # Login credentials and sessions must never reach preferences on disk.
        if any(k in changes for k in ("session_token", "password", "ledger_password")):
            raise DownloadError("登录凭据不能保存到下载设置")
        for key in ("data_root", "ledger_directory"):
            p = Path(value[key])
            if not p.is_absolute() or p == Path(p.anchor):
                raise DownloadError("数据和现场记录需保存到专用文件夹的绝对路径")
        if Path(value["data_root"]).resolve() == Path(value["ledger_directory"]).resolve():
            raise DownloadError("九轴数据根目录和现场记录目录请分别设置")
        if value.get("raw_connection") not in ("authorized_task", "direct_ssh", "http"):
            raise DownloadError("九轴连接方式无效")
        if type(value.get("sync_ledger")) is not bool:
            raise DownloadError("现场记录同步开关无效")
        return super().save(**changes)


def configured_data_root():
    return Path(ProSettings().value['data_root'])
