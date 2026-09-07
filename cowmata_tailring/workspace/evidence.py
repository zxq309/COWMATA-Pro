"""Human-review stills, never model inputs. One original frame per camera.

Blobs are immutable/content-addressed. Labels own the provenance, not filenames.
No video moves, cuts or deletion take place here.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from cowmata_tailring.media.timeline import MediaTimelineIndex

from .catalog import assert_not_being_written, file_stamp
from .clocks import ClockMap, VideoTimeline, intervals_from_rows
from .probe import extract_frame

FOLDER = "证据"
MAX_IMAGE_BYTES = 32 * 1024**2


def safe_relative(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} or ":" in p for p in rel.parts):
        raise ValueError("Invalid evidence-relative path")
    target = root.joinpath(rel)
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Evidence path must not traverse links")
    if not target.resolve().is_relative_to(root):
        raise ValueError("Evidence path escapes its owner")
    return target


def event_context(work, event):
    return {"imu_asset_id": work.asset_id, "cow_id": work.project.cow_id,
            "event_id": event.id, "label_index": event.li, "start_ms": event.t0, "end_ms": event.t1,
            "mapping_revision": work.clock.revision}


def context_matches(bundle, work, event):
    return bundle.get("context") == event_context(work, event)


def suggest_time(motion, start, end):
    """Largest robust acceleration deviation is a suggestion, not a truth rule."""
    end = start if end is None else end
    indices = np.flatnonzero((motion.times_ms >= start) & (motion.times_ms <= end))
    if not len(indices):
        return max(0.0, min(motion.duration_ms, float(start)))
    values = np.stack([motion.channels[key][indices] for key in ("ax", "ay", "az")])
    score = np.sum((values - np.nanmedian(values, axis=1, keepdims=True)) ** 2, axis=0)
    if not np.isfinite(score).any():
        return float(motion.times_ms[indices[len(indices) // 2]])
    return float(motion.times_ms[indices[np.argmax(np.where(np.isfinite(score), score, -np.inf))]])


def capture_frames(root, rows, settings, clock, context, imu_ms, cameras, *, cancelled=lambda: False,
                   extractor=extract_frame):
    cameras = list(dict.fromkeys(cameras))
    if not 1 <= len(cameras) <= 8 or not math.isfinite(imu_ms):
        raise ValueError("请选择 1–8 个视角及有效截图时刻")
    if not context["cow_id"].strip():
        raise ValueError("请先确认目标牛号")
    if not context["start_ms"] <= imu_ms <= (context["end_ms"] if context["end_ms"] is not None else context["start_ms"]):
        raise ValueError("截图时刻必须在所选标签范围内")
    if clock.quality(imu_ms) != "interpolated":
        raise ValueError("截图时刻需在人工校准范围内；设备时间仅用于候选定位")
    maps = {k: ClockMap.from_dict(v) for k, v in settings.get("camera_maps", {}).items()}
    timeline = VideoTimeline(intervals_from_rows(rows, settings.get("camera_overrides")), maps)
    reference = clock.map(imu_ms)

    def capture(camera):
        item = {"camera": camera, "status": "unavailable", "requested_reference_ms": reference}
        if cancelled():
            raise InterruptedError("截图已取消")
        found = timeline.locate(camera, reference)
        if not found:
            return {**item, "reason": "该时刻无录像覆盖"}, None
        interval, target = found
        row = next(r for r in rows if r["path"] == interval.path and r["asset_id"] == interval.asset_id)
        item.update(asset_id=interval.asset_id, video_path=interval.path, requested_media_ms=target,
                    camera_mapping_revision=maps[camera].revision if camera in maps else "uncalibrated",
                    camera_clock_basis="saved_mapping" if camera in maps and maps[camera].anchors else "shared_reference_assumed",
                    video_revision=hashlib.sha256(json.dumps(row["metadata"].get("intervals", []), sort_keys=True).encode()).hexdigest())
        try:
            if not interval.verified:
                raise ValueError("录像时间映射尚未核验")
            path = safe_relative(root, interval.path)
            assert_not_being_written(path)
            before = file_stamp(path)
            if before != row["stamp"]:
                raise ValueError("录像已变化，请刷新索引")
            media = row["metadata"].get("timeline")
            media = MediaTimelineIndex.from_dict(media) if media else None
            frame, actual = extractor(path, target, media, cancelled=cancelled, image_codec="bmp")
            if cancelled():
                raise InterruptedError("截图已取消")
            if before != file_stamp(path):
                raise ValueError("抽帧期间录像发生变化")
            actual_reference = timeline.reference_time(camera, interval.wall_at(actual))
            tolerance = min(500.0, max(100.0, (media.frame_duration_ms if media else 40.0) * 1.5))
            if not interval.media_start <= actual < interval.media_end or abs(actual_reference - reference) > tolerance:
                raise ValueError("实际帧偏离目标时刻或跨越时间缺口，请换一个时刻")
            stream = io.BytesIO()
            frame.convert("RGB").save(stream, format="JPEG", quality=95, subsampling=0)
            payload = stream.getvalue()
            if len(payload) > MAX_IMAGE_BYTES:
                raise ValueError("原始帧过大，未保存")
            sha = hashlib.sha256(payload).hexdigest()
            item.update(status="captured", sha256=sha, path=f"{FOLDER}/{sha}.jpg", bytes=len(payload),
                        width=frame.width, height=frame.height, media_ms=actual, reference_ms=actual_reference,
                        delta_ms=actual_reference - reference, frame_source="original_ffmpeg_pts",
                        source_stamp=before, codec="jpeg_q95_444")
            return item, payload
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            return {**item, "reason": str(exc)}, None

    # Two bounded original-frame decoders, independent of preview/8-player load.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="evidence-frame") as pool:
        results = list(pool.map(capture, cameras))
    bundle = {"schema": 1, "purpose": "human_review_only", "algorithm_input": False,
              "context": copy.deepcopy(context), "imu_ms": imu_ms, "reference_ms": reference,
              "clock": clock.to_dict(), "camera_maps": copy.deepcopy(settings.get("camera_maps", {})),
              "created_utc": datetime.now(timezone.utc).isoformat(), "human_checked": False,
              "items": [r[0] for r in results]}
    return bundle, {item["sha256"]: payload for item, payload in results if payload is not None}


def read_image(root, item):
    if item.get("status") != "captured" or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
        raise ValueError("Invalid evidence image identity")
    path = safe_relative(root, item["path"])
    with path.open("rb") as stream:
        payload = stream.read(MAX_IMAGE_BYTES + 1)
    if len(payload) != item["bytes"] or len(payload) > MAX_IMAGE_BYTES or hashlib.sha256(payload).hexdigest() != item["sha256"]:
        raise ValueError("证据图完整性校验失败")
    with Image.open(io.BytesIO(payload)) as image:
        if image.format != "JPEG" or image.size != (item["width"], item["height"]) or image.width * image.height > 64_000_000:
            raise ValueError("证据图尺寸或格式不符")
        image.verify()
    return payload


def write_blob(root, item, payload):
    if hashlib.sha256(payload).hexdigest() != item["sha256"] or len(payload) != item["bytes"]:
        raise ValueError("Evidence bytes disagree with provenance")
    relative = f"{FOLDER}/{item['sha256']}.jpg"
    target = safe_relative(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        read_image(root, {**item, "path": relative})
    else:
        fd, temporary = tempfile.mkstemp(prefix=".evidence-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Never overwrite a different existing human-owned file.
            try:
                if os.name == "nt":
                    os.rename(temporary, target)  # Atomic, no replacement; works on exFAT too.
                else:
                    os.link(temporary, target)
            except FileExistsError:
                read_image(root, {**item, "path": relative})
        finally:
            Path(temporary).unlink(missing_ok=True)
    item["path"] = relative


def store_bundle(root, bundle, blobs):
    result = copy.deepcopy(bundle)
    for item in result["items"]:
        if item["status"] == "captured":
            write_blob(root, item, blobs[item["sha256"]])
    return result


def copy_evidence(document, source_root, output_root):
    """Copy before committing JSON. A failed export never points to absent images."""
    copied = set()
    for event in document["work"]["project"]["events"]:
        bundle = event.get("screenshots")
        if not bundle:
            continue
        for item in bundle.get("items", []):
            if item.get("status") != "captured":
                continue
            if source_root is None:
                raise ValueError("导出证据图需要原证据目录")
            # Validate each reference even if several events share one blob.
            payload = read_image(source_root, item)
            write_blob(output_root, item, payload)
            copied.add(item["sha256"])
    return len(copied)


def evidence_summary(document, root):
    result = {"saved": 0, "missing": 0, "unavailable": 0, "events_without_images": 0}
    for event in document["work"]["project"]["events"]:
        bundle = event.get("screenshots", {})
        items = bundle.get("items", [])
        if not any(i.get("status") == "captured" for i in items):
            result["events_without_images"] += 1
        for item in items:
            if item.get("status") != "captured":
                result["unavailable"] += 1
                continue
            try:
                read_image(root, item)
                result["saved"] += 1
            except (OSError, ValueError, KeyError):
                result["missing"] += 1
    return result
