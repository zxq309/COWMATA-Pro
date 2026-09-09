from __future__ import annotations

import bisect
import copy
import io
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image

from cowmata_tailring.annotation.data import UnsupportedRecordError, parse_motion_object
from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg, probe_media
from cowmata_tailring.media.native_ps import SIGNATURE as NATIVE_SIGNATURE
from cowmata_tailring.media.native_ps import native_hint, read_native_index
from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.media.timeline import (
    MediaTimelineIndex,
    TimelineSegment,
    probe_media_timeline,
)

from .catalog import file_stamp
from .clocks import wall_text
from .demand import camera_folder
from .ocr import TimestampOCR
from .rapid_backend import TIMESTAMP_SIGNATURE
from .storage import atomic_json, read_json


def extract_frame(path: Path, media_ms: float = 0, timeline: MediaTimelineIndex | None = None, *, preroll=15.0, cancelled=None,
                  preview_width=None, image_codec="png"):
    if image_codec not in {"png", "bmp"}:
        raise ValueError("Unsupported lossless frame transport")
    ffmpeg, _ = find_ffmpeg()
    if timeline and timeline.native:
        native = timeline.native
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (native["source_size"], native["source_mtime_ns"]):
            raise OSError("原生抽帧索引已过期，请刷新素材")
        keys = native["keys"]
        target = max(0, min(float(media_ms), timeline.duration_ms - native["frame_ms"]))
        index = max(0, bisect.bisect_right([k[0] for k in keys], target) - 1)
        base, start_byte = keys[index]
        end_key = bisect.bisect_right([k[0] for k in keys], target + 2000)
        end_byte = keys[end_key][1] if end_key < len(keys) else native["valid_end"]
        if not 0 <= start_byte < end_byte <= stat.st_size or end_byte-start_byte > 64 * 1024 * 1024:
            raise ValueError("原生抽帧窗口无效或过大，需复核")
        with path.open("rb") as stream:
            stream.seek(start_byte)
            payload = stream.read(end_byte-start_byte)
        filters = f"trim=start={max(0, target-base)/1000:.6f},showinfo"
        if preview_width is not None:
            filters += f",scale=w={int(preview_width)}:h=-2:force_original_aspect_ratio=decrease"
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-threads", "2", "-f", "mpeg",
                   "-i", "pipe:0", "-map", "0:v:0", "-an", "-sn", "-vf", filters, "-frames:v", "1",
                   "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", image_codec, "pipe:1"]
        process = run_cancellable(command, timeout=30, cancelled=cancelled, input_data=payload)
        match = re.search(r"\bn:\s*0\b.*?pts_time:([\d.eE+\-]+)", process.stderr.decode("utf-8", "replace"))
        if process.returncode or not process.stdout or not match:
            raise ValueError("原生字节定位未取得可验证画面：" + process.stderr.decode("utf-8", "replace")[-300:])
        actual = base + float(match[1]) * 1000
        if abs(actual - media_ms) > 1500:
            raise ValueError("原生定位帧偏差过大，需复核")
        return Image.open(io.BytesIO(process.stdout)).convert("RGB"), actual
    raw_ms = timeline.public_to_raw_ms(media_ms)[0] if timeline else media_ms
    seek = max(0.0, raw_ms / 1000 - preroll)
    trim = max(0.0, raw_ms / 1000 - seek)
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-threads", "2"]
    if seek:
        command += ["-ss", f"{seek:.6f}"]
    filters = f"trim=start={trim:.6f},showinfo"
    if preview_width is not None:
        filters += f",scale=w={int(preview_width)}:h=-2:force_original_aspect_ratio=decrease"
    command += ["-i", str(path), "-map", "0:v:0", "-an", "-sn",
                "-vf", filters, "-frames:v", "1",
                "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", image_codec, "pipe:1"]
    process = run_cancellable(command, timeout=90, cancelled=cancelled)
    if process.returncode or not process.stdout:
        raise ValueError("无法解码所选位置的真实画面：" + process.stderr.decode("utf-8", "replace")[-400:])
    img = Image.open(io.BytesIO(process.stdout)).convert("RGB")
    match = re.search(r"\bn:\s*0\b.*?pts_time:([\d.eE+\-]+)", process.stderr.decode("utf-8", "replace"))
    if not match:
        raise ValueError("已解码图像但未取得帧时间，不能建立可信定位")
    actual_raw = seek * 1000 + float(match[1]) * 1000
    actual_public = timeline.raw_to_public_ms(actual_raw)[0] if timeline else actual_raw
    if abs(actual_public - media_ms) > 2500:
        raise ValueError(f"抽帧偏离请求 {actual_public - media_ms:.0f} ms，需人工检查时间轴")
    return img, actual_public


def build_observed_intervals(samples: list[dict], timeline: MediaTimelineIndex, *, tolerance_ms=2000):
    """No interpolation through clock jumps, failed samples or packet boundaries."""
    intervals = []
    warnings = []
    ordered = sorted(samples, key=lambda r: r["media_ms"])
    samples = []
    for sample in ordered:
        if samples and sample.get("wall_ms") is not None and sample.get("wall_ms") == samples[-1].get("wall_ms") and sample["media_ms"] - samples[-1]["media_ms"] < 1000:
            continue
        samples.append(sample)
    for left, right in zip(samples, samples[1:]):
        if left.get("wall_ms") is None or right.get("wall_ms") is None:
            warnings.append("有抽帧读数未确认；相邻区间未建立确定映射")
            continue
        dm = right["media_ms"] - left["media_ms"]
        dw = right["wall_ms"] - left["wall_ms"]
        if dm <= 0 or dw <= 0:
            # Same-second early observations are useful votes, not a zero-length
            # mapping. Backward wall jumps must remain unresolved.
            if dw < 0:
                warnings.append("画面时钟倒退，需分段校准")
            continue
        segment_left = timeline.public_to_raw_ms(left["media_ms"])[1]
        segment_right = timeline.public_to_raw_ms(right["media_ms"])[1]
        if abs(dm - dw) > tolerance_ms or segment_left != segment_right:
            warnings.append("画面时间与播放时间非线性或跨 PTS 跳变，保留缺口待复核")
            continue
        intervals.append({"wall_start": left["wall_ms"], "wall_end": right["wall_ms"],
                          "media_start": left["media_ms"], "media_end": right["media_ms"],
                          "verified": True, "warnings": []})
    # Unreadable OSD is not proof of missing footage. Preserve a clearly tentative
    # candidate span only when its packet segment is continuous and endpoint clocks
    # agree. It is browsable but cannot be exported as confirmed video evidence.
    good = [s for s in samples if s.get("wall_ms") is not None]
    for left, right in zip(good, good[1:]):
        if any(left["media_ms"] < s["media_ms"] < right["media_ms"] and s.get("wall_ms") is None for s in samples):
            dm, dw = right["media_ms"] - left["media_ms"], right["wall_ms"] - left["wall_ms"]
            same_segment = timeline.public_to_raw_ms(left["media_ms"])[1] == timeline.public_to_raw_ms(right["media_ms"])[1]
            if dm > 0 and dw > 0 and abs(dm - dw) <= tolerance_ms and same_segment:
                intervals.append({"wall_start": left["wall_ms"], "wall_end": right["wall_ms"],
                                  "media_start": left["media_ms"], "media_end": right["media_ms"],
                                  "verified": False, "warnings": ["中间 OSD 未读清，当前仅为候选时间映射"]})
    # Last decoded frame is an observed one-frame extent, not the header duration.
    if samples and samples[-1].get("wall_ms") is not None and intervals:
        last = samples[-1]
        if abs(intervals[-1]["media_end"] - last["media_ms"]) < 1:
            frame_ms = timeline.frame_duration_ms
            intervals.append({"wall_start": last["wall_ms"], "wall_end": last["wall_ms"] + frame_ms,
                              "media_start": last["media_ms"], "media_end": last["media_ms"] + frame_ms,
                              "verified": True, "warnings": []})
    # Failed edge OCR is not a physical recording gap. Expose only a bounded,
    # explicitly tentative extension, anchored to two observed consistent times.
    # Internal disagreements are deliberately NOT bridged by this fallback.
    for segment in timeline.segments:
        points = [s for s in good if segment.public_start_ms <= s["media_ms"] < segment.public_end_ms]
        if len(points) < 2 or segment.duration_ms <= 0:
            continue
        for first, second, edge in ((points[0], points[1], segment.public_start_ms),
                                     (points[-2], points[-1], segment.public_end_ms)):
            delta = second["media_ms"] - first["media_ms"]
            span = second["wall_ms"] - first["wall_ms"]
            anchor = first if edge < first["media_ms"] else second
            distance = abs(edge - anchor["media_ms"])
            if not 1 < distance <= 301000 or delta <= 0 or abs(delta - span) > tolerance_ms:
                continue
            if any(i["media_start"] <= min(edge, anchor["media_ms"]) and i["media_end"] >= max(edge, anchor["media_ms"]) for i in intervals):
                continue
            media_start, media_end = sorted((edge, anchor["media_ms"]))
            wall_start = anchor["wall_ms"] + (media_start - anchor["media_ms"]) * span / delta
            wall_end = anchor["wall_ms"] + (media_end - anchor["media_ms"]) * span / delta
            warning = "边缘读数未确认，仅供粗定位"
            intervals.append({"wall_start": wall_start, "wall_end": wall_end, "media_start": media_start,
                              "media_end": media_end, "verified": False, "warnings": [warning]})
            warnings.append(warning)
    intervals.sort(key=lambda item: (item["media_start"], not item["verified"]))
    return intervals, sorted(set(warnings))


class SourceInspector:
    def __init__(self, root: Path, meta: Path, stop: threading.Event | None = None, progress=None):
        self.root, self.meta = root, meta
        self.stop = stop or threading.Event()
        self.progress = progress or (lambda _: None)
        self.ocr = None
        self.roi_hints = read_json(self.meta / "ocr_profiles.json", {})
        self.opening_cache = None
        self.last_motion = None
        self.timezone_minutes = int(read_json(self.meta / "project.json", {}).get("timezone_offset_minutes", 480))

    def video_hint(self, path: Path) -> dict:
        """Cheap routing OSD; never a verified interval or evidence identity."""
        hint = native_hint(path, timezone_minutes=self.timezone_minutes, cancelled=self.stop.is_set)
        if hint:
            return hint
        self.ocr = self.ocr or TimestampOCR()
        if hasattr(self.ocr, "engine"):
            self.ocr.engine.cancelled = self.stop.is_set
        self.opening_cache = {"path": path, "stamp": file_stamp(path), "frames": {}}
        points = []
        roi = None
        for target in (0, 1200, 2500):
            if self.stop.is_set():
                raise InterruptedError("时间检索已切换")
            frame, actual = extract_frame(path, target, cancelled=self.stop.is_set)
            report = self.ocr.routing_read(frame, filename=f"{path.name}@hint", hint=roi)
            if not report["success"]:
                report = self.ocr.recognize(frame, filename=f"{path.name}@hint-fallback", hint=roi)
            self.opening_cache["frames"][target] = (frame, actual, report)
            if report.get("success") and report.get("wall_ms") is not None:
                roi = report.get("roi")
                points.append((actual, report["wall_ms"]))
            if len(points) >= 2:
                break
        if len(points) < 2 or abs((points[-1][1] - points[0][1]) - (points[-1][0] - points[0][0])) > 2000:
            return {"start_ms": None, "reason": "首部时间读数待人工核验", "hint_only": True}
        start = sum(w - m for m, w in points) / len(points)
        return {"start_ms": start, "end_ms": None, "hint_only": True, "observations": points}

    def __call__(self, path: Path, kind: str, asset_id: str) -> dict:
        if self.stop.is_set():
            raise RuntimeError("索引已暂停，下次打开会继续")
        if kind == "imu":
            try:
                obj = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(obj, dict):
                    raise UnsupportedRecordError("JSON 顶层必须是对象")
                motion = parse_motion_object(obj, source_path=path.resolve())
            except UnsupportedRecordError as exc:
                return {"ignored": True, "reason": str(exc), "needs_review": False}
            self.last_motion = (path.resolve(), asset_id, motion)
            return {"device": motion.device, "uid": motion.uid, "duration_ms": motion.duration_ms,
                    "samples": len(motion.times_ms), "create_time_ms": motion.create_time_ms,
                    "update_time_ms": motion.update_time_ms, "version": motion.version,
                    "warnings": motion.warnings, "time_semantics": "device_acquisition_start",
                    "capture_timing": motion.capture_timing(), "needs_review": False}
        return self.video(path, asset_id)

    def native_video(self, path, asset_id, native, video, info, roi=None):
        duration = native["duration_ms"]
        stat = path.stat()
        timeline = MediaTimelineIndex(str(path.resolve()), stat.st_size, stat.st_mtime_ns,
                    native["first_pts_ms"], native["frame_ms"],
                    (TimelineSegment(0, duration, 0, duration),), (), native=native)
        deferred = bool(getattr(self, "defer_native_checks", False))
        if not deferred:
            self.ocr = self.ocr or TimestampOCR()
        if not deferred and hasattr(self.ocr, "engine"):
            self.ocr.engine.cancelled = self.stop.is_set
        profile_key = f"native|{path.parent.relative_to(self.root)}|{native['family']}|{video.get('width')}"
        samples, warnings = [], []
        saved_roi = roi or self.roi_hints.get(profile_key) or ((.4, .82, 1, 1) if native["family"].startswith("shenmo") else (0, 0, .8, .18))
        frame_dir = self.meta / "previews"
        frame_dir.mkdir(exist_ok=True)
        if deferred:
            frame, _ = extract_frame(path, 0, timeline, cancelled=self.stop.is_set)
            frame.save(frame_dir / f"{asset_id}.jpg", quality=90)
        # Packet clocks are checked throughout the file above. Only two image
        # observations are needed here to check the private layout/timezone.
        # Packet clocks already cover both ends; central images tend to avoid
        # corrupt opening/closing recorder fragments and speed the visual check.
        for target in (() if deferred else (duration/2, duration/4, duration*3/4, 0.0, max(0, duration-native["frame_ms"]*2))):
            if len([s for s in samples if s.get("wall_ms") is not None]) >= 2:
                break
            self.progress(f"原生时间已读取 · 抽查画面 {path.name} @{target/1000:.1f}s")
            try:
                frame, actual = extract_frame(path, target, timeline, cancelled=self.stop.is_set)
                deadline = time.monotonic() + 12
                if hasattr(self.ocr, "engine"):
                    self.ocr.engine.cancelled = lambda: self.stop.is_set() or time.monotonic() > deadline
                report = self.ocr.native_check(frame, filename=path.name, hint=saved_roi,
                                               family=native['family'] if not roi else None)
                samples.append({"media_ms": actual, "wall_ms": report.get("wall_ms"), "ocr": report})
                if target == 0:
                    frame.save(frame_dir / f"{asset_id}.jpg", quality=90)
                if report.get("success"):
                    saved_roi = report.get("roi") or saved_roi
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                if self.stop.is_set():
                    raise InterruptedError("原生抽查已取消") from exc
                samples.append({"media_ms": target, "wall_ms": None, "error": str(exc)})
            finally:
                if hasattr(self.ocr, "engine"):
                    self.ocr.engine.cancelled = self.stop.is_set
        good = [s for s in samples if s.get("wall_ms") is not None]
        conflict = any(abs(s["wall_ms"] - native["wall_start"] - s["media_ms"]) > 2000 for s in good)
        if conflict:
            warnings.append("原生时间与画面读数冲突；仅供粗定位，请框选时间戳或输入人工读数复核")
        verified = len(good) >= 2 and not conflict
        if good and saved_roi:
            self.roi_hints[profile_key] = saved_roi
            atomic_json(self.meta / "ocr_profiles.json", self.roi_hints)
        if not verified:
            warnings.append("原生时间已读取，画面抽查未全部确认；仅供粗定位，需复核后保存证据")
        start = native["wall_start"]
        folder = camera_folder(path.relative_to(self.root).as_posix())
        corner = "unknown"
        if saved_roi:
            corner = ("top" if (saved_roi[1]+saved_roi[3])/2 < .5 else "bottom") + ("_left" if (saved_roi[0]+saved_roi[2])/2 < .5 else "_right")
        camera = folder if re.match(r"^视角\d", folder) else f"{folder} · {video.get('width')}×{video.get('height')} · {corner}"
        return {"camera": camera,
                "width": video.get("width"), "height": video.get("height"), "codec": video.get("codec_name"),
                "format": info.get("format", {}).get("format_name"), "header_duration": info.get("format", {}).get("duration"),
                "duration_ms": duration, "timeline": timeline.to_dict(), "samples": samples,
                "intervals": [{"wall_start": start, "wall_end": start+duration, "media_start": 0,
                               "media_end": duration, "verified": verified, "warnings": warnings}],
                "roi": saved_roi, "warnings": warnings, "ocr_engine": self.ocr.signature if self.ocr else TIMESTAMP_SIGNATURE,
                "native_check_pending": deferred,
                "time_engine": NATIVE_SIGNATURE, "time_basis": native["family"],
                "preview": f"previews/{asset_id}.jpg", "needs_review": not verified,
                "start_display": wall_text(start, filename=True)}

    def video(self, path: Path, asset_id: str, *, roi=None) -> dict:
        correction = read_json(self.meta / "video_corrections" / (asset_id + ".json"), {})
        roi = roi or correction.get("roi")
        self.progress(f"校验数据包时间轴：{path.name}")
        info = probe_media(path, cancelled=self.stop.is_set)
        video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            raise ValueError("文件中没有可解码的视频流")
        try:
            native = read_native_index(path, timezone_minutes=self.timezone_minutes, cancelled=self.stop.is_set)
        except ValueError as exc:
            self.progress(f"原生时间需复核，使用 OCR：{exc}")
            native = None
        if native:
            result = self.native_video(path, asset_id, native, video, info, roi)
            if result is not None:
                return result
            self.progress("原生时间与画面不一致，转入原有 OCR 分段复核")
        _, ffprobe = find_ffmpeg()
        timeline = probe_media_timeline(path, ffprobe, timeout_seconds=180, cancelled=self.stop.is_set)
        if timeline.duration_ms <= 0:
            raise ValueError("没有有效的视频数据包区间")
        frame_dir = self.meta / "previews"
        frame_dir.mkdir(exist_ok=True)
        self.ocr = self.ocr or TimestampOCR()
        if hasattr(self.ocr, "engine"):
            self.ocr.engine.cancelled = self.stop.is_set
        targets = {0.0, min(1200.0, timeline.duration_ms / 3)}
        for segment in timeline.segments:
            if segment.duration_ms <= 0:
                continue
            if segment.public_start_ms > 0:
                targets.add(segment.public_start_ms + min(50, segment.duration_ms / 4))
            targets.add(max(segment.public_start_ms, segment.public_end_ms - max(2, timeline.frame_duration_ms * 1.5)))
            point = segment.public_start_ms + 300000
            while point < segment.public_end_ms - 1000:
                targets.add(point)
                point += 300000
        samples = []
        layout = "unknown"
        saved_roi = roi
        hint_key = "|".join(map(str, (path.relative_to(self.root).parts[0], video.get("width"), video.get("height"))))
        for number, target in enumerate(sorted(targets)):
            if self.stop.is_set():
                raise RuntimeError("索引已暂停，下次打开会继续")
            self.progress(f"识别 {path.name} · {number + 1}/{len(targets)}")
            try:
                cached = None
                if (self.opening_cache and self.opening_cache["path"] == path
                        and self.opening_cache["stamp"] == file_stamp(path) and not roi
                        and abs(timeline.public_to_raw_ms(target)[0] - target) < 1):
                    cached = self.opening_cache["frames"].get(target)
                if cached:
                    frame, actual, report = cached
                    report = copy.deepcopy(report)
                    if report.get("routing_only"):
                        report = self.ocr.recognize(frame, filename=f"{path.name}@{actual:.0f}ms", roi=roi,
                                                    hint=report.get("roi") or saved_roi, profile_hint=layout)
                else:
                    frame, actual = extract_frame(path, target, timeline, cancelled=self.stop.is_set)
                    report = self.ocr.recognize(frame, filename=f"{path.name}@{actual:.0f}ms", roi=roi,
                                                hint=saved_roi or self.roi_hints.get(hint_key), profile_hint=layout)
                if number == 0:
                    frame.save(frame_dir / f"{asset_id}.jpg", quality=90)
                retries = []
                if not report["success"]:
                    original_report = report
                    for offset in (500, -500, 1500, -1500, -2500, -4000):
                        retry_at = target + offset
                        if not 0 <= retry_at < timeline.duration_ms - timeline.frame_duration_ms:
                            continue
                        retry_frame, retry_actual = extract_frame(path, retry_at, timeline, cancelled=self.stop.is_set)
                        retry_report = self.ocr.recognize(retry_frame, filename=f"{path.name}@{retry_actual:.0f}ms", roi=roi,
                                                          hint=saved_roi, profile_hint=layout)
                        retries.append({"media_ms": retry_actual, "ocr": retry_report})
                        if retry_report["success"]:
                            report, actual = retry_report, retry_actual
                            break
                    report = dict(report)
                    report["original_attempt"] = original_report
                    report["retries"] = retries
                samples.append({"media_ms": actual, "wall_ms": report["wall_ms"], "ocr": report})
                if report["success"]:
                    saved_roi = report["roi"]
                    layout = report["metadata"].get("layout", layout)
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                samples.append({"media_ms": target, "wall_ms": None, "error": str(exc)})
        # A high-confidence glyph can still be wrong. Retry isolated temporal
        # outliers from actual nearby frames, never substitute a predicted digit.
        good = [s for s in samples if s.get("wall_ms") is not None]
        if len(good) >= 3:
            offsets = sorted(s["wall_ms"] - s["media_ms"] for s in good)
            centre = offsets[len(offsets) // 2]
            consensus = sum(abs(offset - centre) <= 2000 for offset in offsets)
            if consensus >= max(3, len(offsets) - 2):
                suspects = [s for s in good if abs(s["wall_ms"] - s["media_ms"] - centre) > 2000]
                for sample in suspects[:2]:
                    for offset in (500, -500, 1500):
                        target = sample["media_ms"] + offset
                        if not 0 <= target < timeline.duration_ms - timeline.frame_duration_ms:
                            continue
                        if self.stop.is_set():
                            raise RuntimeError("索引已暂停")
                        self.progress(f"复核时间推进异常点：{path.name} @{target / 1000:.1f}s")
                        frame, actual = extract_frame(path, target, timeline, cancelled=self.stop.is_set)
                        report = self.ocr.recognize(frame, filename=f"{path.name}@{actual:.0f}ms", roi=roi,
                                                    hint=saved_roi, profile_hint=layout)
                        sample.setdefault("temporal_rechecks", []).append({"media_ms": actual, "ocr": report})
                        if report["wall_ms"] is not None and abs(report["wall_ms"] - actual - centre) <= 2000:
                            sample["original_observation"] = {"media_ms": sample["media_ms"], "wall_ms": sample["wall_ms"], "ocr": sample.get("ocr")}
                            sample.update(media_ms=actual, wall_ms=report["wall_ms"], ocr=report)
                            break
        intervals, warnings = build_observed_intervals(samples, timeline)
        if saved_roi:
            self.roi_hints[hint_key] = saved_roi
            atomic_json(self.meta / "ocr_profiles.json", self.roi_hints)
        relative = path.relative_to(self.root)
        folder = camera_folder(relative.as_posix())
        # Folder is only a tentative grouping; separate visibly different layouts.
        corner = "unknown"
        if saved_roi:
            corner = ("top" if (saved_roi[1] + saved_roi[3]) / 2 < .5 else "bottom") + ("_left" if (saved_roi[0] + saved_roi[2]) / 2 < .5 else "_right")
        camera = folder if re.match(r"^视角\d", folder) else f"{folder} · {video.get('width')}×{video.get('height')} · {corner}"
        valid = [s for s in samples if s.get("wall_ms") is not None]
        if not valid:
            warnings.append("时间戳识别失败：可浏览原片，需框选或输入人工读数")
        return {"camera": camera, "width": video.get("width"), "height": video.get("height"),
                "codec": video.get("codec_name"), "format": info.get("format", {}).get("format_name"),
                "header_duration": info.get("format", {}).get("duration"),
                "duration_ms": timeline.duration_ms, "timeline": timeline.to_dict(),
                "samples": samples, "intervals": intervals, "roi": saved_roi, "warnings": warnings,
                "ocr_engine": self.ocr.signature,
                "time_engine": NATIVE_SIGNATURE,
                "preview": f"previews/{asset_id}.jpg", "needs_review": bool(warnings) or not intervals,
                "start_display": wall_text(valid[0]["wall_ms"], filename=True) if valid else "待确认"}
