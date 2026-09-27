"""Safe DOCX upload staging used by issue/report importers.

DOCX files are ZIP containers.  Treating them as opaque uploads avoids the
path traversal and partially-copied-file failures that previously made valid
bug reports disappear from the intake flow.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


MAX_DOCX_BYTES = 64 * 1024 * 1024
MAX_DOCX_MEMBERS = 10_000
MAX_DOCX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _assert_stable(path: Path, before: tuple[int, int]) -> None:
    try:
        after = _stat_signature(path)
    except OSError as exc:
        raise ValueError("DOCX 上传源已消失") from exc
    if after != before:
        raise ValueError("DOCX 文件仍在上传或写入，请等待完成后重试")


def validate_docx(
    path: str | os.PathLike[str], *,
    max_bytes: int = MAX_DOCX_BYTES,
    max_uncompressed_bytes: int = MAX_DOCX_UNCOMPRESSED_BYTES,
    allow_non_docx_suffix: bool = False,
) -> Path:
    source = Path(path)
    if not allow_non_docx_suffix and source.suffix.casefold() != ".docx":
        raise ValueError("仅支持 DOCX 文件")
    if not source.is_file():
        raise ValueError("DOCX 文件不存在")
    signature = _stat_signature(source)
    if signature[0] > max_bytes:
        raise ValueError("DOCX 文件超过 64 MB 限制")
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) > MAX_DOCX_MEMBERS or len(set(names)) != len(names):
                raise ValueError("DOCX 包含过多或重复文件项")
            total_uncompressed = 0
            for name in names:
                normalized = name.replace("\\", "/")
                item = Path(normalized)
                if "\x00" in name or item.is_absolute() or ".." in item.parts:
                    raise ValueError("DOCX 包含不安全路径")
            for info in archive.infolist():
                total_uncompressed += info.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise ValueError("DOCX 解压后超过安全大小限制")
                # ZIP symlinks can escape the staging directory after extract.
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("DOCX 包含不安全链接")
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise ValueError("DOCX 文件结构不完整")
            if archive.testzip() is not None:
                raise ValueError("DOCX 文件校验失败")
            try:
                ET.fromstring(archive.read("[Content_Types].xml"))
                ET.fromstring(archive.read("word/document.xml"))
            except ET.ParseError as exc:
                raise ValueError("DOCX XML 结构无效") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件损坏或尚未上传完成") from exc
    _assert_stable(source, signature)
    return source


def stage_docx_upload(path: str | os.PathLike[str], destination: str | os.PathLike[str]) -> Path:
    """Validate and atomically copy a DOCX into the destination directory."""
    source = validate_docx(path)
    source_signature = _stat_signature(source)
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    target = target_dir / source.name
    # A same-name upload is idempotent when its content is identical. A
    # different report gets a deterministic suffix instead of clobbering the
    # first report when two uploads arrive together.
    if target.exists():
        existing = _sha256_file(target)
        incoming = _sha256_file(source)
        if incoming == existing:
            return target
        target = target.with_name(f"{target.stem}-{incoming[:8]}{target.suffix}")
    partial = target_dir / f".{target.name}.{uuid.uuid4().hex}.part"
    try:
        with source.open("rb") as src, partial.open("xb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        _assert_stable(source, source_signature)
        validate_docx(partial, allow_non_docx_suffix=True)
        os.replace(partial, target)
        return target
    finally:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass


__all__ = ["MAX_DOCX_BYTES", "MAX_DOCX_MEMBERS", "MAX_DOCX_UNCOMPRESSED_BYTES",
           "stage_docx_upload", "validate_docx"]
