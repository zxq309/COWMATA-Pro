from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

FFMPEG_HOME = Path(r"F:\Applications\ffmpeg-8.1.2-full_build")
FFMPEG = FFMPEG_HOME / "bin" / "ffmpeg.exe"
FFPROBE = FFMPEG_HOME / "bin" / "ffprobe.exe"


class FFmpegToolError(RuntimeError):
    pass


def _candidate_dirs() -> list[Path]:
    """Places to look for ffmpeg.exe/ffprobe.exe, most specific first."""
    dirs: list[Path] = []
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
        raise FFmpegToolError(
            "未找到 ffmpeg.exe/ffprobe.exe：可把 ffmpeg 放进 "
            r"F:\Applications、C:\Applications，或加入 PATH，"
            "或设置 FFMPEG_HOME 环境变量"
        )
    return ffmpeg, ffprobe


def probe_media(path: str | os.PathLike[str]) -> dict[str, Any]:
    _ffmpeg, ffprobe = find_ffmpeg()
    process = subprocess.run(
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
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise FFmpegToolError(
            process.stderr.strip() or "FFprobe 无法识别该文件"
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

