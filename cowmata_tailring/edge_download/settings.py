"""Persistent automatic-download intent; no credentials or animal IDs required."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .core import CHINA, DownloadError, validate_url

DEFAULT_SERVER = "http://127.0.0.1:18031"
DEFAULT_INTERVAL = 900

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def defaults(data_root: Path):
    return {"version": 1, "server": DEFAULT_SERVER, "data_root": str(data_root),
            "start_time": (datetime.now(CHINA) - timedelta(days=7)).replace(microsecond=0).isoformat(),
            "interval_seconds": DEFAULT_INTERVAL, "auto_enabled": True,
            "start_on_login": True, "category": "未分类", "kinds": ["motion"],
            "last_cycle": None, "next_run": None}

def validated(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise DownloadError("自动下载配置格式错误")
    validate_url(value["server"])
    root = Path(value["data_root"])
    if not root.is_absolute() or root == Path(root.anchor):
        raise DownloadError("保存位置必须是专用数据文件夹")
    stamp = datetime.fromisoformat(value["start_time"])
    if stamp.tzinfo is None:
        raise DownloadError("下载起始时间必须带时区")
    if not 60 <= int(value["interval_seconds"]) <= 86400:
        raise DownloadError("自动检查间隔应为1分钟至24小时")
    if type(value.get("auto_enabled")) is not bool or type(value.get("start_on_login")) is not bool:
        raise DownloadError("自动下载开关格式错误")
    allowed = value.get('schema_390') == 1 and isinstance(value.get('kinds'), list) and bool(value['kinds']) and set(value['kinds']) <= {'motion','pulse','temp'}
    if value.get("category") != "未分类" or (value.get("kinds") != ["motion"] and not allowed):
        raise DownloadError("一键模式目前只同步未分类的九轴原始数据")
    return value

class SettingsStore:
    def __init__(self, directory: Path, data_root: Path):
        self.directory = directory.resolve()
        self.path = self.directory / "automatic-download.json"
        self.notice = ""
        if self.path.exists():
            try:
                self.value = validated(self.decode(json.loads(self.path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # Do not discard a customer's settings or silently enable a fresh download.
                raise DownloadError(f"无法读取自动下载配置，请保留文件后检查：{self.path}（{exc}）") from exc
        else:
            self.value = defaults(data_root.resolve())

    def save(self, **changes):
        updated = dict(self.value)
        updated.update(changes)
        updated = validated(updated)
        atomic_json(self.path, self.encode(updated))
        self.value = updated
        return dict(self.value)

    def decode(self, value):
        return value

    def encode(self, value):
        return value

    def record_cycle(self, result, error=""):
        self.save(last_cycle={"finished_at": datetime.now(CHINA).isoformat(),
                              "saved": result.saved if result is not None else 0,
                              "skipped": result.skipped if result is not None else 0,
                              "failed": result.failed if result is not None else 1,
                              "pending": getattr(result, "pending", 0) if result is not None else 0,
                              "recycled_duplicates": getattr(result, "recycled", 0) if result is not None else 0,
                              "canceled": result.canceled if result is not None else False,
                              "error": error}, next_run=None)
