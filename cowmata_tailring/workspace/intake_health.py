"""Confirm blank camera storage slots by reading every byte, then cache identity."""

import hashlib
import json
from pathlib import Path

from . import organization as core
from .storage import atomic_json


def blank_recording(path, cache, cancelled=lambda: False):
    path, cache = Path(path), Path(cache)
    stamp = core.file_stamp(path)
    key = hashlib.sha256(str(path).encode()).hexdigest()
    saved = cache / (key+'.blank-362.json')
    try:
        record = json.loads(saved.read_text(encoding='utf-8'))
        if record.get('stamp') == stamp and record.get('all_zero'):
            return record
    except (OSError, ValueError):
        pass
    with path.open('rb', buffering=0) as stream:
        head = stream.read(128*1024)
        if head.count(0) != len(head):
            return None
        digest = hashlib.sha256(head)
        checked = len(head)
        while block := stream.read(8*1024*1024):
            core.check_cancel(cancelled)
            if block.count(0) != len(block):
                return None
            digest.update(block)
            checked += len(block)
    core.check_cancel(cancelled)
    if core.file_stamp(path) != stamp:
        raise OSError('核验期间文件变化，稍后自动重试')
    record = dict(stamp=stamp, all_zero=True, checked_bytes=checked,
                  sha256=digest.hexdigest(), source=str(path))
    atomic_json(saved, record, backup=False)
    return record


def assess_video(path, cache, cancelled=lambda: False):
    """Bounded visible-content check; deletion still requires full evidence."""
    import re
    import tempfile

    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
    from cowmata_tailring.media.subprocess_tools import run_cancellable

    path, cache = Path(path), Path(cache)
    core.check_cancel(cancelled)
    stamp = core.file_stamp(path)
    key = hashlib.sha256(str(path).encode()).hexdigest()
    saved = cache / (key + ".health-363.json")
    try:
        record = json.loads(saved.read_text(encoding="utf-8"))
        if record.get("stamp") == stamp:
            return record
    except (OSError, ValueError):
        pass
    with core.prevent_writes(path):
        blank = blank_recording(path, cache, cancelled)
        if blank:
            record = {**blank, "delete_reason": "全文件为零或空文件，没有录像内容"}
        else:
            # Retiming is confined to this null-output check. Camera PS clocks
            # may otherwise cause output timestamp errors on valid recordings.
            ffmpeg = find_ffmpeg()[0]
            with tempfile.TemporaryFile() as preview_out, tempfile.TemporaryFile() as preview_err:
                quick = run_cancellable(
                    [
                        str(ffmpeg),
                        "-hide_banner",
                        "-nostdin",
                        "-v",
                        "error",
                        "-threads",
                        "1",
                        "-filter_threads",
                        "1",
                        "-i",
                        str(path),
                        "-map",
                        "0:v:0",
                        "-an",
                        "-sn",
                        "-dn",
                        "-frames:v",
                        "32",
                        "-vf",
                        "scale=64:64",
                        "-pix_fmt",
                        "gray",
                        "-f",
                        "rawvideo",
                        "pipe:1",
                    ],
                    timeout=45,
                    cancelled=cancelled,
                    stdout_file=preview_out,
                    stderr_file=preview_err,
                )
                preview_out.seek(0)
                pixels = preview_out.read(32 * 4096)
            if quick.returncode == 0 and len(pixels) >= 4096 and max(pixels) >= 18:
                if core.file_stamp(path) != stamp:
                    raise OSError("核验期间文件变化，保留原件并重试")
                record = dict(
                    stamp=stamp,
                    source=str(path),
                    decoded_frames=len(pixels) // 4096,
                    delete_reason="",
                    decoder_returncode=0,
                    health_scope="opening_visible",
                    full_decode=False,
                )
                atomic_json(saved, record, backup=False)
                return record

            from collections import deque

            environmental = (
                "permission denied",
                "input/output error",
                "resource temporarily unavailable",
                "cannot allocate memory",
                "no such file",
                "unknown decoder",
                "decoder not found",
                "error while opening decoder",
                "option not found",
                "no such filter",
            )
            content_markers = (
                "invalid data found",
                "moov atom not found",
                "error during demuxing",
                "error submitting packet",
                "error while decoding",
                "does not contain any stream",
                "matches no streams",
            )
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="health-log-", dir=cache) as logs:
                output, errors = Path(logs) / "progress.txt", Path(logs) / "decoder.txt"
                with output.open("wb") as stdout, errors.open("wb") as stderr:
                    result = run_cancellable(
                        [
                            str(ffmpeg),
                            "-hide_banner",
                            "-nostdin",
                            "-v",
                            "info",
                            "-nostats",
                            "-threads",
                            "1",
                            "-filter_threads",
                            "1",
                            "-filter_complex_threads",
                            "1",
                            "-i",
                            str(path),
                            "-map",
                            "0:v:0",
                            "-an",
                            "-sn",
                            "-dn",
                            "-vf",
                            "setpts=N/(25*TB),blackframe=amount=100:threshold=18",
                            "-enc_time_base",
                            "1:25",
                            "-fps_mode",
                            "passthrough",
                            "-progress",
                            "pipe:1",
                            "-f",
                            "null",
                            "-",
                        ],
                        timeout=24 * 3600,
                        cancelled=cancelled,
                        stdout_file=stdout,
                        stderr_file=stderr,
                    )
                core.check_cancel(cancelled)
                count = 0
                with output.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        if match := re.match(r"^frame=(\d+)", line):
                            count = max(count, int(match[1]))
                tail = deque(maxlen=30)
                environmental_error = content_error = False
                black_count, contiguous = 0, True
                with errors.open(encoding="utf-8", errors="replace") as stream:
                    while line := stream.readline(8192):
                        core.check_cancel(cancelled)
                        tail.append(line)
                        lower = line.lower()
                        environmental_error |= any(word in lower for word in environmental)
                        content_error |= any(word in lower for word in content_markers)
                        if match := re.search(r"blackframe[^\n]*frame:(\d+) pblack:100", line):
                            contiguous &= int(match[1]) == black_count
                            black_count += 1
                log = "".join(tail)[-800:]
            if environmental_error:
                raise OSError("读取环境异常，保留原件并可重试：" + log)
            reason = ""
            if result.returncode and content_error:
                reason = "录像结构损坏或无法解码"
            elif result.returncode or not count:
                raise OSError("未能完成视频核验，保留原件并可重试：" + log)
            elif contiguous and black_count == count:
                reason = "整段解码确认全程黑屏"
            record = dict(
                stamp=stamp,
                source=str(path),
                decoded_frames=count,
                delete_reason=reason,
                decoder_returncode=result.returncode,
                health_scope="full_decode",
                full_decode=True,
            )
        if core.file_stamp(path) != stamp:
            raise OSError("核验期间文件变化，保留原件并重试")
    atomic_json(saved, record, backup=False)
    return record
