"""MP4 preparation with measured packet clocks and cancellable helpers."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.media.timeline import probe_media_timeline

from .dahua_source import TZ, check


def run(command, cancelled, timeout=3600):
    try:
        result = run_cancellable([str(v) for v in command], cancelled=cancelled, timeout=timeout)
    except RuntimeError:
        check(cancelled)
        raise
    check(cancelled)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace")[-2400:] or "媒体处理失败")
    return result


def probe(path, cancelled=lambda: False, *, dav=False):
    _, ffprobe = find_ffmpeg()
    args = ["-f", "dhav"] if dav else []
    result = run(
        [ffprobe, "-v", "error", *args, "-show_streams", "-show_format", "-of", "json", path],
        cancelled,
        120,
    )
    value = json.loads(result.stdout)
    video = next((s for s in value.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or not video.get("width") or not video.get("height"):
        raise ValueError("未发现可解码视频流")
    value["video"] = video
    return value


def packet_clock(path, cancelled=lambda: False, *, dav=False):
    _, ffprobe = find_ffmpeg()
    args = ["-f", "dhav"] if dav else []
    value = run(
        [
            ffprobe,
            "-v",
            "error",
            *args,
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "compact=p=0:nk=0",
            path,
        ],
        cancelled,
        300,
    )
    first = last = maximum = None
    duplicates = backwards = count = 0
    max_back = max_gap = duration = 0.0
    for line in value.stdout.decode("utf-8", "replace").splitlines():
        fields = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
        try:
            pts = float(fields["pts_time"])
        except (ValueError, KeyError):
            raise ValueError("码流缺少逐帧 PTS，不能根据猜测帧率归档") from None
        if not math.isfinite(pts):
            raise ValueError("码流 PTS 无效")
        if first is None:
            first = pts
        if last is not None:
            delta = pts - last
            duplicates += delta == 0
            backwards += delta < 0
            max_back = max(max_back, -delta)
            max_gap = max(max_gap, delta)
        last = pts
        maximum = pts if maximum is None else max(maximum, pts)
        count += 1
        try:
            duration = float(fields.get("duration_time", 0))
        except ValueError:
            duration = 0
    if count < 2 or maximum <= first:
        raise ValueError("视频帧不足或 PTS 不递增")
    if max_back > 0.25 or max_gap > 5:
        raise ValueError(
            f"视频时钟不连续（回退 {max_back:.3f}s / 间隔 {max_gap:.3f}s），需单独复核"
        )
    return dict(
        first=first,
        last=maximum,
        duration=maximum - first + max(duration, 0),
        packets=count,
        duplicates=duplicates,
        backwards=backwards,
        max_backwards_seconds=max_back,
        max_gap_seconds=max_gap,
    )


def time_ms(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("时间无效")
        return int(value)
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def segments(start, end, requested_start=None, requested_end=None, split_midnight=True):
    lo, hi = time_ms(requested_start), time_ms(requested_end)
    if lo is not None and hi is not None and hi <= lo:
        raise ValueError("结束时间必须晚于开始时间")
    first = max(start, lo) if lo is not None else start
    last = min(end, hi) if hi is not None else end
    result = []
    while first < last:
        dt = datetime.fromtimestamp(first / 1000, TZ)
        midnight = datetime.combine(dt.date() + timedelta(days=1), datetime.min.time(), TZ)
        boundary = min(last, int(midnight.timestamp() * 1000)) if split_midnight else last
        result.append((int(first), int(boundary)))
        first = boundary
    return result


def transcode(source, target, offset_ms, duration_ms, cancelled=lambda: False, *, stage=lambda *_a, **_k: None):
    """Keep compatible H.264 video packets; exact middle cuts use the encoder."""
    import os
    import uuid
    from pathlib import Path

    target = Path(target)
    if target.exists():
        raise FileExistsError("目标 MP4 已存在，未覆盖：" + str(target))
    if offset_ms < 0 or duration_ms <= 0:
        raise ValueError("转码时间范围无效")
    if offset_ms == 0:
        stage_path = target.with_name(target.stem + ".remux-" + uuid.uuid4().hex + ".mp4")
        try:
            video = probe(source, cancelled, dav=True)["video"]
            if (
                video.get("codec_name") == "h264"
                and video.get("pix_fmt") == "yuv420p"
                and video.get("color_range") != "pc"
            ):
                ffmpeg, _ = find_ffmpeg()
                stage("convert", "快速封装 MP4（保留视频码流）", method="stream_copy")
                result = run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-v",
                        "warning",
                        "-xerror",
                        "-copyts",
                        "-start_at_zero",
                        "-f",
                        "dhav",
                        "-i",
                        source,
                        "-t",
                        f"{duration_ms / 1000:.6f}",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-map_metadata",
                        "-1",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-ar",
                        "48000",
                        "-b:a",
                        "96k",
                        "-movflags",
                        "+faststart",
                        "-n",
                        stage_path,
                    ],
                    cancelled,
                    max(300, duration_ms / 1000 * 5),
                )
                verified = _validate_output(stage_path, duration_ms, result, "stream_copy", cancelled, stage=stage)
                check(cancelled)
                if os.name == "nt":
                    os.rename(stage_path, target)
                else:
                    os.link(stage_path, target)
                    stage_path.unlink()
                verified["info"].setdefault("format", {})["filename"] = str(target)
                return verified
        except (ValueError, OSError):
            check(cancelled)
            if target.exists():
                raise
        finally:
            stage_path.unlink(missing_ok=True)
    return encode_with_fallback(source, target, offset_ms, duration_ms, cancelled, stage)


_encoder_cache = {}


def encoder_options(encoder):
    if encoder == "h264_nvenc":
        return ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0", "-bf", "0"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast", "-global_quality", "23", "-bf", "0"]
    return ["-preset", "veryfast", "-crf", "23"]


def available_encoder(cancelled=lambda: False):
    """Probe an actual encode, not merely the presence of a compiled codec."""
    import os
    ffmpeg, _ = find_ffmpeg()
    key = str(ffmpeg)
    if os.name != "nt":
        return "libx264"
    if key not in _encoder_cache:
        selected = "libx264"
        for encoder in ("h264_qsv", "h264_nvenc"):
            check(cancelled)
            try:
                run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
                     "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25",
                     "-frames:v", "5", "-an", "-c:v", encoder, *encoder_options(encoder),
                     "-f", "null", "-"], cancelled, 12)
                selected = encoder
                break
            except (ValueError, OSError, RuntimeError):
                check(cancelled)
        _encoder_cache[key] = selected
    return _encoder_cache[key]


def encode_with_fallback(source, target, offset_ms, duration_ms, cancelled, stage):
    import os
    import uuid
    from pathlib import Path
    target = Path(target)
    encoder = available_encoder(cancelled)
    if encoder != "libx264":
        temporary = target.with_name(target.stem + ".hardware-" + uuid.uuid4().hex + ".mp4")
        try:
            result = _encode(source, temporary, offset_ms, duration_ms, cancelled,
                             encoder=encoder, stage=stage)
            check(cancelled)
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
                temporary.unlink()
            result["info"].setdefault("format", {})["filename"] = str(target)
            return result
        except (OSError, ValueError, RuntimeError):
            check(cancelled)
            if target.exists():
                raise
            # Don't repeatedly spend time on an unavailable/busy encoder.
            ffmpeg, _ = find_ffmpeg()
            _encoder_cache[str(ffmpeg)] = "libx264"
            stage("convert", "硬件加速不可用，自动回退 CPU 编码", method="libx264")
        finally:
            temporary.unlink(missing_ok=True)
    return _encode(source, target, offset_ms, duration_ms, cancelled, stage=stage)


def _encode(source, target, offset_ms, duration_ms, cancelled=lambda: False, *, encoder="libx264", stage=lambda *_a, **_k: None):
    ffmpeg, _ = find_ffmpeg()
    stage("convert", "硬件编码 H.264" if encoder != "libx264" else "CPU 编码 H.264", method=encoder)
    # No guessed frame rate: VFR keeps input timestamps, dropping duplicates.
    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "warning",
        "-xerror",
        "-err_detect",
        "explode",
        "-threads",
        "2",
        "-copyts",
        "-start_at_zero",
        "-f",
        "dhav",
        "-i",
        source,
        "-ss",
        f"{offset_ms / 1000:.6f}",
        "-t",
        f"{duration_ms / 1000:.6f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-vf",
        "scale=in_range=auto:out_range=tv",
        "-c:v",
        encoder,
        *encoder_options(encoder),
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",
        "-fps_mode",
        "vfr",
        "-enc_time_base",
        "1:1000",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-n",
        target,
    ]
    result = run(args, cancelled, max(300, duration_ms / 1000 * 10))
    verified = _validate_output(target, duration_ms, result, "encoded", cancelled, stage=stage)
    verified["settings"].update(encoder=encoder, hardware_accelerated=encoder != "libx264",
                                crf=23 if encoder == "libx264" else None,
                                hardware_quality=23 if encoder != "libx264" else None)
    return verified


def _validate_output(target, duration_ms, result, processing, cancelled, *, stage=lambda *_a, **_k: None):
    ffmpeg, _ = find_ffmpeg()
    stage("verify", "完整解码校验 MP4")
    info = probe(target, cancelled)
    video = info["video"]
    if video["codec_name"] != "h264" or video.get("pix_fmt") != "yuv420p":
        raise ValueError("派生 MP4 编码不符合标准")
    run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "2",
            "-i",
            target,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-f",
            "null",
            "-",
        ],
        cancelled,
        max(300, duration_ms / 1000 * 5),
    )
    _, ffprobe = find_ffmpeg()
    stage("verify", "核对成品时间轴")
    timeline = probe_media_timeline(target, ffprobe, cancelled=cancelled)
    if not 0 < timeline.duration_ms <= duration_ms + 1500:
        raise ValueError("转码后视频时长超出所选区间")
    return dict(
        info=info,
        timeline=timeline.to_dict(),
        duration_ms=timeline.duration_ms,
        log=result.stderr.decode("utf-8", "replace")[-4000:],
        settings=dict(
            video="h264",
            pixel_format="yuv420p",
            audio="aac",
            pts="vfr",
            crf=23 if processing == "encoded" else None,
            video_processing=processing,
            original_resolution=True,
        ),
    )


def thumbnail(source, target, cancelled=lambda: False, *, dav=False):
    ffmpeg, _ = find_ffmpeg()
    args = ["-f", "dhav"] if dav else []
    run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            *args,
            "-i",
            source,
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-2",
            "-n",
            target,
        ],
        cancelled,
        90,
    )
    return str(target)
