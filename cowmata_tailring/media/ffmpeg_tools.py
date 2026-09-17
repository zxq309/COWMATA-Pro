from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

FFMPEG_HOME = Path(r"F:\Applications\ffmpeg-8.1.2-full_build")
FFMPEG = FFMPEG_HOME / "bin" / "ffmpeg.exe"
FFPROBE = FFMPEG_HOME / "bin" / "ffprobe.exe"


class FFmpegToolError(RuntimeError):
    pass


def _candidate_dirs() -> list[Path]:
    """Places to look for ffmpeg.exe/ffprobe.exe, most specific first."""
    dirs: list[Path] = [Path(__file__).resolve().parents[2] / "vendor" / "ffmpeg"]
    for env_name in ("FFMPEG_HOME", "FFMPEG_DIR"):
        value = os.environ.get(env_name)
        if value:
            dirs.append(Path(value))
    dirs.append(FFMPEG_HOME)
    for root in (r"F:\Applications", r"C:\Applications", r"D:\Applications"):
        base = Path(root)
        if base.is_dir():
            dirs.extend(
                p for p in sorted(base.glob("ffmpeg*")) if p.is_dir()
            )
    return dirs


def find_ffmpeg() -> tuple[Path, Path]:
    ffmpeg = Path("")
    ffprobe = Path("")
    for base in _candidate_dirs():
        for sub in (base / "bin", base):
            if not ffmpeg.is_file() and (sub / "ffmpeg.exe").is_file():
                ffmpeg = sub / "ffmpeg.exe"
            if not ffprobe.is_file() and (sub / "ffprobe.exe").is_file():
                ffprobe = sub / "ffprobe.exe"
        if ffmpeg.is_file() and ffprobe.is_file():
            break
    if not ffmpeg.is_file():
        ffmpeg = Path(shutil.which("ffmpeg") or "")
    if not ffprobe.is_file():
        ffprobe = Path(shutil.which("ffprobe") or "")
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise FFmpegToolError("便携包缺少 ffmpeg.exe/ffprobe.exe，请重新解压完整软件包（vendor/ffmpeg），无需安装系统依赖")
    return ffmpeg, ffprobe


def probe_media(path: str | os.PathLike[str], *, cancelled=None) -> dict[str, Any]:
    from .subprocess_tools import run_cancellable

    _ffmpeg, ffprobe = find_ffmpeg()
    process = run_cancellable(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=format_name,format_long_name,duration,size,bit_rate:"
            "stream=index,codec_type,codec_name,profile,codec_tag_string,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,duration",
            "-of",
            "json",
            os.fspath(path),
        ],
        timeout=30,
        cancelled=cancelled,
    )
    if process.returncode != 0:
        raise FFmpegToolError(
            process.stderr.decode("utf-8", "replace").strip() or "FFprobe 无法识别该文件"
        )
    return json.loads(process.stdout)


def cache_target(
    source: str | os.PathLike[str], cache_root: str | os.PathLike[str]
) -> Path:
    source_path = Path(source).resolve()
    stat = source_path.stat()
    digest = hashlib.sha1(
        f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}".encode(
            "utf-8", errors="surrogatepass"
        )
    ).hexdigest()[:16]
    return Path(cache_root) / f"{source_path.stem}.{digest}.browser.mkv"


def streamcopy_command(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
) -> list[str]:
    ffmpeg, _ffprobe = find_ffmpeg()
    return [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        os.fspath(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-c",
        "copy",
        os.fspath(target),
    ]
