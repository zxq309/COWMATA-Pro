"""MP4 preparation with measured packet clocks and cancellable helpers."""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.subprocess_tools import run_cancellable, run_progress
from cowmata_tailring.media.timeline import probe_media_timeline

from .dahua_source import TZ, check


def run(command, cancelled, timeout=3600, *, progress=None):
    try:
        if progress is None:
            result = run_cancellable([str(v) for v in command], cancelled=cancelled, timeout=timeout)
        else:
            result = run_progress([str(v) for v in command], cancelled=cancelled, timeout=timeout, progress=progress)
    except RuntimeError:
        check(cancelled)
        raise
    check(cancelled)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace")[-2400:] or "媒体处理失败")
    return result


def media_progress(stage, phase, title, duration_ms):
    def update(values):
        def number(name):
            try:
                value = float(values.get(name, 0))
                return value if math.isfinite(value) else 0
            except (ValueError, TypeError):
                return 0
        percent = min(100, max(0, number('out_time_us') / 1000 / duration_ms * 100))
        fps, speed = number('fps'), values.get('speed', '')
        frames = int(number('frame'))
        details = dict(media_percent=round(percent, 1), frames=frames, fps=fps, media_speed=speed)
        if number('total_size') > 0:
            details['output_bytes'] = int(number('total_size'))
        stage(phase, f'{title} {percent:.1f}% · {frames} 帧 · {fps:.1f} fps · {speed}', **details)
    return update


_decode_threads = ContextVar("dahua_decode_threads", default=None)


@contextmanager
def decoder_budget(threads):
    token = _decode_threads.set(threads)
    try:
        yield
    finally:
        _decode_threads.reset(token)


def verification_threads():
    # The private worker already owns a limited CPU affinity and job budget.
    # Bound decoding threads too; never consume every logical CPU on a desktop.
    return _decode_threads.get() or max(1, min(8, (os.cpu_count() or 2) // 2))


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


def recorded_clock(path, info, cancelled=lambda: False):
    """Recover only a proved continuous DHAV counter; never infer a frame rate.

    The packed calendar can jump while the recorder's 16-bit millisecond
    counter and frame sequence stay continuous. Keep calendar steps as evidence.
    Preserve measured video counter deviations. PCM sample time is used only
    with consecutive packet numbers and a bounded, recorded clock discrepancy.
    """
    import mmap
    import struct
    from fractions import Fraction

    from .dahua_source import MAX_PACKET, packed_ms

    if info.get("video", {}).get("has_b_frames") != 0:
        return None
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    audio_width = {"pcm_alaw": 1, "pcm_mulaw": 1, "pcm_s8": 1, "pcm_s16le": 2}
    if audio and audio.get("codec_name") not in audio_width:
        return None
    rate = int(audio.get("sample_rate", 0)) if audio else 0
    width = audio_width.get(audio.get("codec_name"), 0) * int(audio.get("channels", 0)) if audio else 0
    if audio and (rate <= 0 or width <= 0):
        return None
    try:
        declared_step = 1000 / Fraction(info.get("video", {}).get("r_frame_rate", "0/1"))
        declared_step = int(declared_step) if declared_step.denominator == 1 and 20 <= declared_step <= 200 else None
    except (ValueError, ZeroDivisionError):
        declared_step = None
    previous = {}
    video_count = audio_count = 0
    first_tick = first_wall = previous_wall = step = None
    elapsed = audio_elapsed = samples = 0
    audio_first = None
    events = []
    corrections = []
    audio_max_error = audio_packet_ms = 0
    with open(path, "rb") as stream:
        if not stream.seek(0, 2):
            return None
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            pos = 0
            while pos < len(data):
                if (video_count + audio_count) % 256 == 0:
                    check(cancelled)
                if pos + 32 > len(data) or data[pos:pos+4] != b"DHAV":
                    return None
                frame, size, date, tick = struct.unpack_from("<IIIH", data, pos + 8)
                if not 32 <= size <= MAX_PACKET or pos + size > len(data):
                    return None
                if data[pos+size-8:pos+size-4] != b"dhav" or struct.unpack_from("<I", data, pos+size-4)[0] != size:
                    return None
                kind = data[pos+4]
                if kind in (0xfc, 0xfd, 0xf0):
                    key = "audio" if kind == 0xf0 else "video"
                    last = previous.get(key)
                    delta = (tick-last[1]) % 65536 if last else 0
                    if last and ((frame-last[0]) % 4294967296 != 1 or not (0 <= delta <= 1000 if key == "audio" else 0 < delta <= 1000)):
                        return None
                    previous[key] = (frame, tick)
                    wall = packed_ms(date)
                    if key == "video":
                        if first_tick is None:
                            first_tick, first_wall = tick, wall
                        else:
                            step = (declared_step or delta) if step is None else step
                            if delta != step:
                                if declared_step is None or len(corrections) >= 64:
                                    return None
                                corrections.append([video_count, delta - step])
                            elapsed += delta
                            if abs(wall - (first_wall + elapsed)) > 5000:
                                return None
                            if wall < previous_wall or wall - previous_wall > 1000:
                                events.append(dict(media_ms=elapsed, wall_ms=wall,
                                                   calendar_step_ms=wall-previous_wall))
                        previous_wall = wall
                        video_count += 1
                    else:
                        if not audio or first_tick is None:
                            return None
                        payload = size - 32 - data[pos+22]
                        if payload <= 0 or payload % width:
                            return None
                        if audio_first is None:
                            audio_first = ((tick-first_tick+32768) % 65536)-32768
                            if abs(audio_first) > 2000 or abs(wall-first_wall) > 2000:
                                return None
                        else:
                            audio_elapsed += delta
                            error = abs(audio_elapsed - samples * 1000 / rate)
                            audio_max_error = max(audio_max_error, error)
                            # At most five source PCM packets; never infer missing audio.
                            if error > max(40, min(200, audio_packet_ms * 5)):
                                return None
                        audio_packet_ms = max(audio_packet_ms, payload / width * 1000 / rate)
                        samples += payload / width
                        audio_count += 1
                pos += size
    if video_count < 2 or step is None or audio and not audio_count:
        return None
    duration = elapsed + step
    if audio and abs(audio_first + samples * 1000 / rate - duration) > max(80, min(200, audio_packet_ms * 5)):
        return None
    return dict(first=0, last=elapsed/1000, duration=duration/1000,
                packets=video_count, duplicates=0, backwards=0,
                max_backwards_seconds=0, max_gap_seconds=max([step, *(step + delta for _, delta in corrections)])/1000,
                recovery=dict(method="validated_dhav_counter", frame_interval_ms=step,
                              video_frames=video_count, audio_offset_ms=audio_first,
                              duration_ms=duration, video_clock_corrections=corrections,
                              audio_samples=int(samples), audio_sample_rate=rate,
                              audio_max_clock_error_ms=audio_max_error,
                              audio_end_offset_ms=(audio_first + samples * 1000 / rate - duration) if audio else None,
                              wall_clock_events=events, first_wall_ms=first_wall,
                              wall_precision_ms=1000))


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


def transcode(source, target, offset_ms, duration_ms, cancelled=lambda: False, *, stage=lambda *_a, **_k: None, timing=None):
    """Keep compatible H.264/HEVC packets; exact middle cuts use the encoder."""
    import os
    import uuid
    from pathlib import Path

    target = Path(target)
    if target.exists():
        raise FileExistsError("目标 MP4 已存在，未覆盖：" + str(target))
    if offset_ms < 0 or duration_ms <= 0:
        raise ValueError("转码时间范围无效")
    copy_failure = ""
    full_record = timing and duration_ms >= timing.get("duration_ms", timing["video_frames"] * timing["frame_interval_ms"])
    if offset_ms == 0 and (not timing or full_record):
        stage_path = target.with_name(target.stem + ".remux-" + uuid.uuid4().hex + ".mp4")
        try:
            video = probe(source, cancelled, dav=True)["video"]
            if (
                video.get("codec_name") in {"h264", "hevc"}
                and video.get("pix_fmt") in {"yuv420p", "yuvj420p"}
            ):
                ffmpeg, _ = find_ffmpeg()
                copy_timing = timing
                if copy_timing is None:
                    stage("convert", "快速封装 MP4（保留视频码流）", method="stream_copy")
                if copy_timing:
                    stage("convert", "快速封装 MP4（按原始计数校时，保留视频码流）", method="stream_copy")
                result = run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-v",
                        "warning",
                        "-xerror",
                        "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
                        "-copyts",
                        "-start_at_zero",
                        "-f",
                        "dhav",
                        "-i",
                        source,
                        *([] if full_record else ["-t", f"{duration_ms / 1000:.6f}"]),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-map_metadata",
                        "-1",
                        "-c:v",
                        "copy",
                        *(["-tag:v", "hvc1"] if video["codec_name"] == "hevc" else []),
                        *(["-bsf:v", video_clock_filter(copy_timing)] if copy_timing else []),
                        *audio_clock_options(copy_timing),
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
                    progress=media_progress(stage, "convert", "快速封装", duration_ms),
                )
                verified = _validate_output(stage_path, duration_ms, result, "stream_copy", cancelled, stage=stage, timing=copy_timing, source_video=video)
                check(cancelled)
                if os.name == "nt":
                    os.rename(stage_path, target)
                else:
                    os.link(stage_path, target)
                    stage_path.unlink()
                verified["info"].setdefault("format", {})["filename"] = str(target)
                return verified
        except (ValueError, OSError) as exc:
            copy_failure = str(exc)[-2400:]
            check(cancelled)
            if target.exists():
                raise
        finally:
            stage_path.unlink(missing_ok=True)
    if copy_failure:
        stage("convert", "快速封装校验未通过，改用编码转换", method="encoded")
    result = encode_with_fallback(source, target, offset_ms, duration_ms, cancelled, stage, timing=timing)
    if copy_failure:
        result.setdefault("settings", {})["stream_copy_fallback_reason"] = copy_failure
    return result


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


def encode_with_fallback(source, target, offset_ms, duration_ms, cancelled, stage, *, timing=None):
    import os
    import uuid
    from pathlib import Path
    target = Path(target)
    encoder = available_encoder(cancelled)
    if encoder != "libx264":
        temporary = target.with_name(target.stem + ".hardware-" + uuid.uuid4().hex + ".mp4")
        try:
            result = _encode(source, temporary, offset_ms, duration_ms, cancelled,
                             encoder=encoder, stage=stage, **({"timing": timing} if timing else {}))
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
    return _encode(source, target, offset_ms, duration_ms, cancelled, stage=stage, **({"timing": timing} if timing else {}))


def video_clock_expression(timing, variable="N"):
    expression = f"{variable}*{timing['frame_interval_ms']}"
    for frame, delta in timing.get("video_clock_corrections", []):
        expression += rf"+({delta})*gte({variable}\,{frame})"
    return "(" + expression + ")" if timing.get("video_clock_corrections") else expression


def video_clock_filter(timing):
    duration = str(timing["frame_interval_ms"])
    for frame, delta in timing.get("video_clock_corrections", []):
        duration += rf"+({delta})*eq(N\,{frame - 1})"
    return f"setts=ts={video_clock_expression(timing)}/1000/TB:duration=({duration})/1000/TB"


def expected_video_frames(timing, offset_ms, duration_ms):
    corrections = iter(timing.get("video_clock_corrections", []))
    upcoming = next(corrections, None)
    adjustment = count = 0
    for frame in range(timing["video_frames"]):
        if upcoming and upcoming[0] == frame:
            adjustment += upcoming[1]
            upcoming = next(corrections, None)
        at = frame * timing["frame_interval_ms"] + adjustment
        count += offset_ms <= at < offset_ms + duration_ms
    return count


def audio_clock_options(timing):
    if not timing:
        return []
    if timing.get("audio_offset_ms") is None:
        return []
    return ["-af", f"asetpts=N/SR/TB+{timing['audio_offset_ms']}/(1000*TB)"]


def _encode(source, target, offset_ms, duration_ms, cancelled=lambda: False, *, encoder="libx264", stage=lambda *_a, **_k: None, timing=None):
    """Try a complete QSV decode/VPP/encode pipeline, then retain software fallback."""
    import uuid
    from pathlib import Path
    if encoder == 'h264_qsv':
        temporary = Path(target).with_name(Path(target).stem + '.decode-' + uuid.uuid4().hex + '.mp4')
        try:
            info = probe(source, cancelled, dav=True)['video']
            # Full-range camera frames need the proved CPU range conversion.
            # Some Intel drivers expose VPP range options but leave pixel levels unchanged.
            if (info.get('codec_name') in {'hevc', 'h264'}
                    and info.get('pix_fmt') in {'yuv420p', 'nv12'}
                    and info.get('color_range') == 'tv'):
                result = _encode_attempt(source, temporary, offset_ms, duration_ms, cancelled, encoder=encoder,
                                         stage=stage, timing=timing, decoder=info['codec_name'] + '_qsv')
                check(cancelled)
                if os.name == 'nt':
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
                    temporary.unlink()
                result['info'].setdefault('format', {})['filename'] = str(target)
                return result
        except (OSError, ValueError, RuntimeError):
            check(cancelled)
            stage('convert', 'Intel 硬件解码不可用，保留硬件编码并回退软件解码', method=encoder)
        finally:
            temporary.unlink(missing_ok=True)
    return _encode_attempt(source, target, offset_ms, duration_ms, cancelled, encoder=encoder, stage=stage, timing=timing)


def _encode_attempt(source, target, offset_ms, duration_ms, cancelled=lambda: False, *, encoder="libx264", stage=lambda *_a, **_k: None, timing=None, decoder=None):
    ffmpeg, _ = find_ffmpeg()
    title = "Intel 硬件解码＋编码" if decoder else "硬件编码 H.264" if encoder != "libx264" else "CPU 编码 H.264"
    stage("convert", title, method=encoder)
    # Only proved continuous counters permit timestamps from frame/sample counts.
    # Other formats retain their measured presentation timestamps.
    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "warning",
        "-xerror",
        "-err_detect",
        "explode",
        "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
        *(["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", decoder] if decoder else ["-threads", str(verification_threads())]),
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
        (f"settb=1/1000,setpts={video_clock_expression(timing)}/(1000*TB)," if timing else "") + ("vpp_qsv=format=nv12:out_range=tv" if decoder else "scale=in_range=auto:out_range=tv"),
        *audio_clock_options(timing),
        "-c:v",
        encoder,
        *encoder_options(encoder),
        *([] if decoder else ["-pix_fmt", "yuv420p"]),
        "-threads",
        "2",
        "-fps_mode",
        "passthrough" if timing else "vfr",
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
    result = run(args, cancelled, max(300, duration_ms / 1000 * 10),
                 progress=media_progress(stage, "convert", title, duration_ms))
    verified = _validate_output(target, duration_ms, result, "encoded", cancelled, stage=stage, timing=timing, offset_ms=offset_ms)
    verified["settings"].update(encoder=encoder, decoder=decoder or "software",
                                hardware_decoded=bool(decoder), hardware_accelerated=encoder != "libx264",
                                crf=23 if encoder == "libx264" else None,
                                hardware_quality=23 if encoder != "libx264" else None)
    return verified


def _validate_output(target, duration_ms, result, processing, cancelled, *, stage=lambda *_a, **_k: None, timing=None, offset_ms=0, source_video=None):
    ffmpeg, _ = find_ffmpeg()
    info = probe(target, cancelled)
    video = info["video"]
    if processing == "stream_copy":
        # Container conversion must preserve the original video, including full range.
        # A successful mux alone does not establish a valid lossless result.
        fields = ("codec_name", "pix_fmt", "color_range", "width", "height")
        if not source_video or any(video.get(key) != source_video.get(key) for key in fields):
            raise ValueError("快速封装改变了原视频编码、色彩或分辨率")
    elif video["codec_name"] != "h264" or video.get("pix_fmt") != "yuv420p":
        raise ValueError("派生 MP4 编码不符合标准")
    frames = None
    verification_mode = "full_decode"
    if processing == "stream_copy":
        # The supplied Dahua clips share a stable HEVC/H.264 + PCM-A-law
        # contract.  A second full decode of every copied clip only repeats
        # the expensive operation.  Count packets and decode short boundary
        # samples instead; encoded fallbacks retain the full pass below.
        verification_mode = "quick_samples"
        stage("verify", "快速校验（容器、时间轴、首尾采样）")
        if timing:
            clock = packet_clock(target, cancelled)
            frames = clock["packets"]
            expected = expected_video_frames(timing, offset_ms, duration_ms)
            if frames != expected:
                raise ValueError(f"转封装后帧数不一致（预期 {expected}，实际 {frames}），已停止归档")
        _sample_decode(target, duration_ms, cancelled)
    else:
        stage("verify", "完整解码校验 MP4")
        decoded = run(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-threads",
                str(verification_threads()),
                "-i",
                target,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-fps_mode",
                "passthrough",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "null",
                "-",
            ],
            cancelled,
            max(300, duration_ms / 1000 * 5),
            progress=media_progress(stage, "verify", "完整校验", duration_ms),
        )
        if timing:
            values = [line.split("=", 1)[1] for line in decoded.stdout.decode("utf-8", "replace").splitlines()
                      if line.startswith("frame=")]
            frames = int(values[-1]) if values else None
            expected = expected_video_frames(timing, offset_ms, duration_ms)
            if frames != expected:
                raise ValueError(f"转码后帧数不一致（预期 {expected}，实际 {frames}），已停止归档")
    _, ffprobe = find_ffmpeg()
    stage("verify", "核对成品时间轴")
    timeline = probe_media_timeline(target, ffprobe, cancelled=cancelled)
    if timing and abs(timeline.duration_ms - duration_ms) > 250:
        raise ValueError("校时后成品时长与原始录像计数不一致")
    if not 0 < timeline.duration_ms <= duration_ms + 1500:
        raise ValueError("转码后视频时长超出所选区间")
    return dict(
        info=info,
        timeline=timeline.to_dict(),
        duration_ms=timeline.duration_ms,
        log=result.stderr.decode("utf-8", "replace")[-4000:],
        settings=dict(
            video=video["codec_name"],
            pixel_format=video["pix_fmt"],
            color_range=video.get("color_range"),
            audio="aac",
            pts=(timing.get("method") if timing else "vfr"),
            verified_video_frames=frames,
            crf=23 if processing == "encoded" else None,
            video_processing=processing,
            verification_mode=verification_mode,
            original_resolution=True,
        ),
    )


def _sample_decode(target, duration_ms, cancelled):
    """Decode only the beginning and end of a stream-copy result."""
    ffmpeg, _ = find_ffmpeg()
    windows = [("首段", 0)]
    if duration_ms > 5000:
        windows.append(("尾段", -2))
    for _title, seek in windows:
        args = [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode", "-threads", "1"]
        args.extend(["-sseof", str(seek)] if seek < 0 else ["-ss", str(seek)])
        args.extend(["-i", target, "-map", "0:v:0", "-t", "2", "-an", "-f", "null", "-"])
        run(args, cancelled, 90)


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
