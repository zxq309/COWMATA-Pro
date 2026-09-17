"""Read-only video archive verification. Never copies or deletes recordings."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .catalog import EXCLUDE_DIRS, VIDEO_SUFFIXES, assert_not_being_written, file_stamp
from .evidence import safe_relative


def verified_digest(path, *, cancelled=lambda: False):
    assert_not_being_written(path)
    before = file_stamp(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024**2):
            if cancelled():
                raise InterruptedError("归档核验已取消；未移动或删除任何文件")
            digest.update(block)
    if before != file_stamp(path):
        raise ValueError("核验时文件发生变化")
    return digest.hexdigest()


def video_files(root, *, cancelled=lambda: False):
    def failed(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
        if cancelled():
            raise InterruptedError("归档核验已取消")
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not (Path(directory) / d).is_symlink()
                   and not getattr(Path(directory) / d, "is_junction", lambda: False)()]
        for name in names:
            path = Path(directory) / name
            if path.suffix.lower() in VIDEO_SUFFIXES and not path.is_symlink():
                yield path


def verify_archive(root, rows, archive_root, *, cancelled=lambda: False, progress=lambda message: None):
    root, archive_root = Path(root).resolve(), Path(archive_root).resolve()
    if not archive_root.is_dir() or root == archive_root or archive_root.is_relative_to(root) or root.is_relative_to(archive_root):
        raise ValueError("归档目录须在数据工程之外，不能选择原目录或其父子目录")
    videos = [r for r in rows if r["kind"] == "video" and r["state"] != "missing"]
    if not videos:
        raise ValueError("没有本机录像可核验；请在删除本机原片之前运行")
    expected_paths = {r["path"] for r in videos}
    unindexed = [p.relative_to(root).as_posix() for p in video_files(root, cancelled=cancelled)
                 if p.relative_to(root).as_posix() not in expected_paths]
    sizes = {}
    for row in videos:
        try:
            size = safe_relative(root, row["path"]).stat().st_size
            sizes.setdefault(size, []).append(row)
        except OSError:
            pass
    candidates = {}
    progress("正在扫描归档目录（只检查录像）…")
    for path in video_files(archive_root, cancelled=cancelled):
        size = path.stat().st_size
        if size in sizes:
            candidates.setdefault(size, []).append(path)
    cache, results = {}, []
    for index, row in enumerate(videos):
        if cancelled():
            raise InterruptedError("归档核验已取消")
        progress(f"正在核对 {index + 1}/{len(videos)}：{row['path']}")
        result = {"asset_id": row["asset_id"], "source_path": row["path"], "status": "failed"}
        try:
            if row["state"] not in {"ready", "review"} or not row["asset_id"]:
                raise ValueError("原录像索引尚未完成，请先刷新")
            source = safe_relative(root, row["path"])
            if file_stamp(source) != row["stamp"] or verified_digest(source, cancelled=cancelled) != row["asset_id"]:
                raise ValueError("原片与索引身份不一致")
            matched = None
            options = sorted(candidates.get(source.stat().st_size, []), key=lambda p: (p.name != source.name, str(p)))
            for path in options:
                stamp = file_stamp(path)
                if path not in cache or cache[path][0] != stamp:
                    cache[path] = (stamp, verified_digest(path, cancelled=cancelled))
                if cache[path][1] == row["asset_id"]:
                    if os.path.samefile(source, path):
                        raise ValueError("归档仍指向原文件，不是独立副本")
                    matched = path
                    break
            if matched is None:
                raise ValueError("未找到内容完全一致的归档副本")
            result.update(status="verified", archive_path=matched.relative_to(archive_root).as_posix(),
                          bytes=source.stat().st_size, source_stamp=row["stamp"], archive_stamp=file_stamp(matched))
        except (OSError, ValueError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            result["reason"] = str(exc)
        results.append(result)
    return {"schema": 1, "kind": "cowmata-video-archive", "archive_root_hint": str(archive_root),
            "verified_utc": datetime.now(timezone.utc).isoformat(), "source_root_hint": str(root),
            "all_verified": not unindexed and all(r["status"] == "verified" for r in results),
            "unindexed_videos": unindexed, "files": results, "deletion_performed": False,
            "playback_spot_checked": False}


def archived_asset(report, asset_id):
    return any(r.get("asset_id") == asset_id and r.get("status") == "verified" for r in report.get("files", []))
