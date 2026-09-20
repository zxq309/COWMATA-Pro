"""Safe DOCX upload staging used by issue/report importers.

DOCX files are ZIP containers.  Treating them as opaque uploads avoids the
path traversal and partially-copied-file failures that previously made valid
bug reports disappear from the intake flow.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path


MAX_DOCX_BYTES = 64 * 1024 * 1024


def validate_docx(path: str | os.PathLike[str], *, max_bytes: int = MAX_DOCX_BYTES) -> Path:
    source = Path(path)
    if source.suffix.casefold() != ".docx":
        raise ValueError("仅支持 DOCX 文件")
    if not source.is_file():
        raise ValueError("DOCX 文件不存在")
    if source.stat().st_size > max_bytes:
        raise ValueError("DOCX 文件超过 64 MB 限制")
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            for name in names:
                item = Path(name)
                if item.is_absolute() or ".." in item.parts:
                    raise ValueError("DOCX 包含不安全路径")
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise ValueError("DOCX 文件结构不完整")
            if any(info.filename.endswith("/") and info.file_size for info in archive.infolist()):
                raise ValueError("DOCX 目录项无效")
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件损坏或尚未上传完成") from exc
    return source


def stage_docx_upload(path: str | os.PathLike[str], destination: str | os.PathLike[str]) -> Path:
    """Validate and atomically copy a DOCX into the destination directory."""
    source = validate_docx(path)
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    partial = target.with_name(target.name + ".part")
    shutil.copyfile(source, partial)
    os.replace(partial, target)
    return target


__all__ = ["MAX_DOCX_BYTES", "stage_docx_upload", "validate_docx"]
