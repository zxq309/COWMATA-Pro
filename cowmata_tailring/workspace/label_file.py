"""One movable annotation JSON; coordinates always belong to the parent IMU.

Full exports embed the original JSON bytes; snippets embed original frames,
not resampled plotted values. Source
names and server timestamps are hints, never identities or alignment anchors.
Loading history is read-only, including the project's SQLite index.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from cowmata_tailring.annotation.core import Project
from cowmata_tailring.annotation.data import load_motion_json, parse_motion_object

from .catalog import (
    META_DIR,
    assert_not_being_written,
    bind_location_metadata,
    digest_file,
    file_stamp,
)
from .clocks import ClockMap, VideoTimeline, intervals_from_rows
from .storage import atomic_json
from .work import SessionWork

FORMAT = "cowmata-annotation"


def contained(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("Source path escapes the selected project")
    return path


def _overlap(start, end, lo, hi):
    return start <= hi and (end if end is not None else start) >= lo


def build_label_file(work, motion, root, rows, settings, *, selection=None, include_record=True):
    root = Path(root).resolve()
    snapshot = copy.deepcopy(work.to_dict())
    lo, hi = 0.0, motion.duration_ms
    embedded = None
    content = None
    if selection is not None or include_record:
        assert_not_being_written(motion.source_path)
        before = file_stamp(motion.source_path)
        content = motion.source_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != work.asset_id or before != file_stamp(motion.source_path):
            raise ValueError("IMU source changed; refresh and verify before export")
    if selection is None and include_record:
        # One payload, byte-for-byte original including all auxiliary channels
        # and unknown metadata. Never duplicate it as both parsed JSON and blob.
        embedded = {"kind": "original_json", "original_json_base64": base64.b64encode(content).decode("ascii"),
                    "sha256": work.asset_id, "parent_start_ms": 0,
                    "sample_start": 0, "sample_stop": motion.sample_count, "acc_scale": motion.acc_scale}
    if selection is not None:
        lo, hi = sorted(map(float, selection))
        if not math.isfinite(lo + hi) or lo < 0 or hi > motion.duration_ms or hi <= lo:
            raise ValueError("Invalid snippet range")
        first, stop = np.searchsorted(motion.times_ms, [lo, hi], side="left")
        stop = int(np.searchsorted(motion.times_ms, hi, side="right"))
        first = int(first)
        if stop - first < 2:
            raise ValueError("Select at least two actual IMU samples")
        obj = json.loads(content.decode("utf-8-sig"))
        raw = base64.b64decode("".join(obj["imu"].split()), validate=True)
        if len(raw) != motion.sample_count * motion.frame_bytes:
            raise ValueError("IMU source and loaded sample count disagree")
        frames = raw[first * motion.frame_bytes:stop * motion.frame_bytes]
        # Auxiliary buckets have a separate time base; do not stretch them over
        # the snippet or pretend they were sampled at the nine-axis rate.
        obj.pop("temperature", None)
        obj.pop("motion", None)
        obj["imu"] = base64.b64encode(frames).decode("ascii")
        obj["version"] = motion.version
        lo, hi = float(motion.times_ms[first]), float(motion.times_ms[stop - 1])
        embedded = {"record": obj, "frames_sha256": hashlib.sha256(frames).hexdigest(),
                    "parent_start_ms": lo, "sample_start": first, "sample_stop": stop,
                    "acc_scale": motion.acc_scale, "auxiliary_channels": "not_included"}
        snapshot["project"]["events"] = [e for e in snapshot["project"]["events"]
                                                if _overlap(e["t0"], e.get("t1"), lo, hi)]
        if work.clock.anchors:
            snapshot["drafts"] = [d for d in snapshot["drafts"] if _overlap(
                work.clock.map(d["reference_start"], inverse=True),
                work.clock.map(d["reference_end"], inverse=True) if d.get("reference_end") is not None else None,
                lo, hi)]
    maps = {k: ClockMap.from_dict(v) for k, v in settings.get("camera_maps", {}).items()}
    timeline = VideoTimeline(intervals_from_rows(rows, settings.get("camera_overrides")), maps)
    video_ids = {e.get("asset_id") for d in work.drafts for e in d.get("video_evidence", [])}
    video_ids.update(e.get("asset_id") for event in work.project.events for e in event.extras.get("video_evidence", []))
    if work.clock.anchors:
        start, end = work.clock.map(lo), work.clock.map(hi)
        video_ids.update(s.asset_id for s in timeline.intervals if _overlap(
            timeline.reference_time(s.camera, s.wall_start), timeline.reference_time(s.camera, s.wall_end), start, end))
    return {"format": FORMAT, "version": 2 if embedded and embedded.get("kind") == "original_json" else 1,
            "coordinates": "parent_imu_ms", **work.category_fields(),
            "work": snapshot, "view": {"start_ms": lo, "end_ms": hi},
            "source": {"asset_id": work.asset_id, "path": motion.source_path.resolve().relative_to(root).as_posix(),
                       "project_root_hint": str(root), "acc_scale": motion.acc_scale,
                       "capture_timing": motion.capture_timing()},
            "video": {"rows": [copy.deepcopy(r) for r in rows if r["kind"] == "video" and r["asset_id"] in video_ids],
                      "archive": copy.deepcopy(settings.get("video_archive", {})),
                      "camera_maps": copy.deepcopy(settings.get("camera_maps", {})),
                      "camera_overrides": copy.deepcopy(settings.get("camera_overrides", {})),
                      "selected_cameras": list(settings.get("selected_cameras", []))},
            "embedded_imu": embedded}


def save_label_file(path, document, *, protected=(), evidence_root=None):
    path = Path(path).resolve()
    if path in {Path(p).resolve() for p in protected}:
        raise ValueError("Cannot overwrite an original source or working annotation")
    if path.suffix.lower() != ".json":
        raise ValueError("Annotation output must be a JSON file")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(existing, dict) or existing.get("format") != FORMAT or existing.get("source", {}).get("asset_id") != document["source"]["asset_id"]:
            raise ValueError("Refusing to overwrite raw data or another recording's annotation")
    from .evidence import copy_evidence
    document = copy.deepcopy(document)
    copy_evidence(document, evidence_root, path.parent)
    atomic_json(path, document)


def read_label_file(path):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Not an annotation document")
    if data.get("format") == FORMAT:
        if data.get("version") not in {1, 2} or data.get("coordinates") != "parent_imu_ms":
            raise ValueError("Unsupported annotation version or coordinates")
        work = SessionWork.from_dict(data["work"])
        if data["source"]["asset_id"] != work.asset_id:
            raise ValueError("Annotation source identities disagree")
        lo, hi = (float(data["view"][key]) for key in ("start_ms", "end_ms"))
        if not math.isfinite(lo + hi) or lo < 0 or hi < lo:
            raise ValueError("Invalid annotation view range")
        return data
    # Previous workspace exports and old single-video project JSON remain
    # readable. Missing identity/alignment is explicit, not filled from names.
    if "project" in data and "asset_id" in data:
        work = SessionWork.from_dict(data)
    elif "labels" in data and "events" in data and "imu" not in data:
        project = Project.from_dict(data)
        work = SessionWork(str(project.source.get("asset_id", "")), project,
                           ClockMap.from_dict(project.align.get("workspaceClock", {})))
    else:
        raise ValueError("Not an annotation document; open raw IMU through a data project")
    end = max([float(work.project.source.get("durationMs") or 0)] +
              [e.t1 if e.t1 is not None else e.t0 for e in work.project.events])
    return {"format": FORMAT, "version": 1, "coordinates": "parent_imu_ms", "legacy": True,
            "work": work.to_dict(), "source": {"asset_id": work.asset_id, "path": work.project.source.get("path", "")},
            "view": {"start_ms": 0, "end_ms": end}, "video": {}, "embedded_imu": None}


def read_index(root):
    path = Path(root) / META_DIR / "index.sqlite"
    if not path.is_file():
        return [], {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        rows = [{**dict(r), "metadata": json.loads(r["metadata"] or "{}")} for r in db.execute(
            "SELECT l.*,a.metadata FROM locations l LEFT JOIN assets a ON l.asset_id=a.id ORDER BY l.path")]
    settings_file = path.parent / "project.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8-sig")) if settings_file.is_file() else {}
    return rows, settings


@dataclass
class HistoryData:
    document: dict
    work: SessionWork
    motion: object
    root: Path | None
    rows: list
    timeline: VideoTimeline
    warnings: list[str]


def load_history(path, root=None, *, cancelled=lambda: False):
    doc = read_label_file(path)
    work = SessionWork.from_dict(doc["work"])
    hint = doc["source"].get("project_root_hint", "")
    root = Path(root).resolve() if root else Path(hint).resolve() if hint and Path(hint).is_dir() else None
    warnings = []
    from .evidence import evidence_summary
    stills = evidence_summary(doc, Path(path).parent)
    if stills["saved"]:
        warnings.append(f"已校验证据图 {stills['saved']} 张（仅供人工回看，不参与算法）")
    if stills["missing"]:
        warnings.append(f"{stills['missing']} 张证据图缺失或损坏；请把标注 JSON 与“证据”文件夹一起复制")
    try:
        rows, settings = read_index(root) if root else ([], {})
    except (OSError, ValueError, sqlite3.Error):
        rows, settings = [], {}
        warnings.append("当前索引无法读取；改用历史快照核验，原索引不会被修改。")
    motion = None
    embedded = doc.get("embedded_imu")
    if embedded:
        if embedded.get("kind") == "original_json":
            content = base64.b64decode(embedded["original_json_base64"], validate=True)
            if hashlib.sha256(content).hexdigest() != work.asset_id or embedded["sha256"] != work.asset_id:
                raise ValueError("Embedded original JSON failed source identity validation")
            record = json.loads(content.decode("utf-8-sig"))
        else:
            record = embedded["record"]
            frames = base64.b64decode(record["imu"], validate=True)
            if hashlib.sha256(frames).hexdigest() != embedded["frames_sha256"]:
                raise ValueError("Embedded IMU frames failed checksum validation")
        motion = parse_motion_object(record, source_path=Path(path), acc_scale=embedded["acc_scale"])
        offset = float(embedded["parent_start_ms"])
        if offset != doc["view"]["start_ms"] or abs(offset + motion.duration_ms - doc["view"]["end_ms"]) > .001:
            raise ValueError("Snippet offset does not match its parent view range")
        # Snippet counters retain their original absolute elapsed values. Shift
        # only the label coordinate system, not the device epoch a second time.
        timing = doc["source"].get("capture_timing", {})
        if timing:
            motion = replace(motion, first_frame_elapsed_ms=float(timing.get("first_frame_elapsed_ms", 0))
                             + offset - float(timing.get("coordinate_offset_ms", 0)))
        motion = replace(motion, times_ms=motion.times_ms + offset, duration_ms=offset + motion.duration_ms,
                         coordinate_offset_ms=offset)
    elif root and len(work.asset_id) == 64:
        paths = [r["path"] for r in rows if r["kind"] == "imu" and r["asset_id"] == work.asset_id]
        paths.append(doc["source"].get("path", ""))
        for relative in dict.fromkeys(paths):
            if cancelled():
                raise InterruptedError("History load cancelled")
            try:
                source = contained(root, relative)
                before = file_stamp(source)
                assert_not_being_written(source)
                if digest_file(source) != work.asset_id:
                    continue
                motion = load_motion_json(source, acc_scale=doc["source"].get("acc_scale", 4096))
                if before != file_stamp(source):
                    motion = None
                    continue
                break
            except OSError:
                continue
    if motion is None:
        warnings.append("未找到身份匹配的九轴原件；仍可查看标签，请重新选择数据工程。")
    if not work.clock.anchors and motion is not None:
        work.clock = ClockMap.from_capture(motion, settings.get("timezone_offset_minutes", 480))
    if work.clock.basis != "manual":
        warnings.append("按设备采集时间定位候选录像；未替代人工相机校准，历史标签保持原状。")
    elif not work.clock.anchors:
        warnings.append("没有可用九轴采集时间或校准锚点；不会用文件名或服务器收包时间对齐视频。")
    saved = doc.get("video", {})
    maps = {k: ClockMap.from_dict(v) for k, v in {**settings.get("camera_maps", {}), **saved.get("camera_maps", {})}.items()}
    overrides = {**settings.get("camera_overrides", {}), **saved.get("camera_overrides", {})}
    saved_rows = saved.get("rows", [])
    if saved_rows:
        candidates = []
        for old in saved_rows:
            # Keep the archived clock/OCR mapping. Only relocate by content ID.
            matches = [r for r in rows if r["kind"] == "video" and r["asset_id"] == old["asset_id"]]
            archive_matches = [r for r in saved.get("archive", {}).get("files", [])
                               if r.get("asset_id") == old["asset_id"] and r.get("status") == "verified"]
            candidates.extend([{**old, "path": r["path"], "stamp": r["stamp"]} for r in matches] or [old])
            candidates.extend({**old, "path": r["archive_path"], "stamp": "archive_requires_sha256"} for r in archive_matches)
        if saved.get("camera_maps", {}) != settings.get("camera_maps", {}):
            warnings.append("回看使用标注文件保存的相机校准版本，不会静默迁移历史标签。")
    else:
        # Old exports lack a media snapshot; use only indexed matching time.
        tl = VideoTimeline(intervals_from_rows(rows, overrides), maps)
        ids = set()
        if work.clock.anchors:
            lo, hi = (work.clock.map(doc["view"][key]) for key in ("start_ms", "end_ms"))
            ids = {s.asset_id for s in tl.intervals if _overlap(tl.reference_time(s.camera, s.wall_start), tl.reference_time(s.camera, s.wall_end), lo, hi)}
        candidates = [r for r in rows if r["kind"] == "video" and r["asset_id"] in ids]
    usable, seen = [], set()
    for row in candidates if root else []:
        if cancelled():
            raise InterruptedError("History load cancelled")
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        try:
            source = contained(root, row["path"])
            before = file_stamp(source)
            assert_not_being_written(source)
            # Stored stamps are only a fast path at the original root. A moved
            # project is checked against SHA-256 before archived timing is used.
            if root != Path(hint) or before != row["stamp"]:
                if digest_file(source) != row["asset_id"]:
                    raise OSError("Different video content")
            if before != file_stamp(source):
                raise OSError("Changing video")
            usable.append({**row, "stamp": before, "metadata": bind_location_metadata(row["metadata"], before)})
        except OSError:
            warnings.append("录像缺失或已变化：" + row["path"])
    timeline = VideoTimeline(intervals_from_rows(usable, overrides), maps)
    if not timeline.intervals:
        warnings.append("原录像当前不在本机或不可用；可回看标签和已保存证据图，不能仅凭截图重新确认整段动作。")
    return HistoryData(doc, work, motion, root, usable, timeline, warnings)
