"""Optional bounded, lossless working-set remux for legacy surveillance PS.

Only containers change in this derived cache; video/audio codecs are copied.
Originals and human work are never evicted. A discontinuous source is rejected
unless the remux can be verified to preserve its exposed playback clock.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.media.timeline import MediaTimelineIndex, probe_media_timeline

from .catalog import file_stamp
from .storage import atomic_json, read_json


class CompatibilityCache:
    def __init__(self, meta: Path, *, max_bytes=8 * 1024 ** 3, readonly=False):
        self.root = meta / "cache" / "compatibility"
        self.max_bytes = max_bytes
        self.readonly = readonly
        self.lock = threading.RLock()
        self.entries = read_json(self.root / "manifest.json", {})

    def cached(self, asset_id):
        with self.lock:
            entry = self.entries.get(asset_id)
            if entry:
                path = self.root / (asset_id + ".mkv")
                try:
                    if file_stamp(path) == entry["stamp"]:
                        entry["last_use"] = time.time()
                        return path
                except OSError:
                    pass
        return None

    def build(self, asset_id, source, metadata, *, cancelled=None, pinned=()):
        if len(asset_id) != 64 or any(c not in "0123456789abcdef" for c in asset_id):
            raise ValueError("非法缓存素材身份")
        with self.lock:
            cached = self.cached(asset_id)
            if cached:
                return cached
            if self.readonly:
                raise ValueError("只读工程尚无此录像的兼容缓存，请关闭其他写入窗口后再创建")
            source_index = MediaTimelineIndex.from_dict(metadata["timeline"])
            if len([s for s in source_index.segments if s.duration_ms > 0]) != 1:
                raise ValueError("该片存在多段 PTS 跳变，不能套用单段兼容缓存；请使用原片或先人工核验")
            required = int(source.stat().st_size * 1.1) + 1024 * 1024
            self.root.mkdir(parents=True, exist_ok=True)
            self._make_room(required, pinned)
            target = self.root / (asset_id + ".mkv")
            temporary = self.root / (asset_id + ".building.mkv")
            before = file_stamp(source)
            ffmpeg, ffprobe = find_ffmpeg()
            command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
                       "-i", str(source), "-t", f"{source_index.duration_ms / 1000:.6f}",
                       "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                       "-avoid_negative_ts", "make_zero", "-y", str(temporary)]
            try:
                process = run_cancellable(command, timeout=180, cancelled=cancelled)
                if process.returncode:
                    raise ValueError("兼容封装失败：" + process.stderr.decode("utf-8", "replace")[-300:])
                if file_stamp(source) != before:
                    raise ValueError("创建缓存时原片发生变化，结果不会被使用")
                index = probe_media_timeline(temporary, ffprobe, timeout_seconds=120, cancelled=cancelled)
                tolerance = max(2, source_index.frame_duration_ms * 1.1)
                if abs(index.duration_ms - source_index.duration_ms) > tolerance or index.is_corrected or abs(index.first_pts_ms) > tolerance:
                    raise ValueError("兼容文件的时间轴与原片不一致，已拒绝使用；不会静默修改同步关系")
                if cancelled and cancelled():
                    raise RuntimeError("兼容缓存请求已取消")
                os.replace(temporary, target)
                self.entries[asset_id] = {"stamp": file_stamp(target), "size": target.stat().st_size,
                                          "last_use": time.time(), "source_stamp": before,
                                          "source_duration_ms": source_index.duration_ms,
                                          "cache_duration_ms": index.duration_ms, "method": "stream_copy"}
                atomic_json(self.root / "manifest.json", self.entries)
                return target
            finally:
                if temporary.exists():
                    temporary.unlink()  # Only this exact owned incomplete cache.

    def _make_room(self, required, pinned):
        if required > self.max_bytes:
            raise ValueError("单个文件超过兼容缓存容量限制，请调大上限或使用原片")
        pinned = {Path(p).resolve() for p in pinned}
        total = sum(e["size"] for e in self.entries.values())
        for asset_id, entry in sorted(list(self.entries.items()), key=lambda item: item[1]["last_use"]):
            if total + required <= self.max_bytes:
                break
            path = (self.root / (asset_id + ".mkv")).resolve()
            if not path.is_relative_to(self.root.resolve()) or path in pinned:
                continue
            if path.is_file():
                path.unlink()
            total -= entry["size"]
            del self.entries[asset_id]
        if total + required > self.max_bytes:
            raise ValueError("兼容缓存容量不足且现有文件仍在播放，不能清理正在使用的缓存")
