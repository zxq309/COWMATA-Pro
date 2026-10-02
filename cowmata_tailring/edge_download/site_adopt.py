"""4.4.6: move files that earlier versions saved outside the site tree into it.

Earlier defaults kept downloads and 台账 in a folder beside the app (``<安装位置的上一级>\\COWMATA Pro 数据``) or
in Documents. When the settings switch to the tree (``pro_settings``), the next download round first moves
those files to the same relative place in the tree, so nothing is downloaded twice and nothing stays behind.
A file already in the tree with identical bytes is dropped from the old folder; a different one is left in
the old folder and reported (never overwritten). Emptied folders are removed, including the data folder
beside the app.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .paths import DATA_DIRECTORY, FARM_MARKER

STATE = ".edge-download"
PROVENANCE = "csv-provenance.jsonl"


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.digest()


def _same(a, b):
    return a.stat().st_size == b.stat().st_size and _digest(a) == _digest(b)


def _move_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)  # same drive: one rename, nothing copied
        return
    except OSError:
        if target.exists():
            raise
    tmp = target.with_name(target.name + ".adopt-tmp")
    shutil.copy2(source, tmp)
    if not _same(source, tmp):
        tmp.unlink(missing_ok=True)
        raise OSError(f"复制核对失败：{source}")
    os.replace(tmp, target)
    source.unlink()


def _remove_empty(folder, stop):
    """Remove empty folders below ``folder`` and then ``folder`` and its parents up to (excluding) ``stop``."""
    for child in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            child.rmdir()
        except OSError:
            pass
    current = folder
    while current != stop and current != Path(current.anchor):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def adopt_folder(old, new, log=lambda message: None):
    """Move every file of ``old`` to the same relative path under ``new``. Returns counts."""
    old, new = Path(old), Path(new)
    counts = dict(moved=0, same=0, kept=0)
    if not old.is_dir() or old.resolve() == new.resolve() or new.resolve().is_relative_to(old.resolve()):
        return counts
    new.mkdir(parents=True, exist_ok=True)
    state, target_state = old / STATE, new / STATE
    if state.is_dir():
        if not target_state.exists():
            # The download state belongs to these files (paths are relative to the root): keep it with them.
            target_state.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(state, target_state)
            except OSError:
                shutil.copytree(state, target_state)
                shutil.rmtree(state)
        else:
            # The tree already has its own state; keep the provenance of the files moved in, drop the rest
            # (completion caches are rebuilt by the next round from the files themselves).
            provenance = state / PROVENANCE
            if provenance.is_file():
                with (target_state / PROVENANCE).open("ab") as stream:
                    stream.write(provenance.read_bytes())
            shutil.rmtree(state)
    for file in sorted(p for p in old.rglob("*") if p.is_file()):
        relative = file.relative_to(old)
        target = new / relative
        if relative.name == FARM_MARKER and len(relative.parts) == 1 and target.exists():
            file.unlink()  # the tree's farm keeps its own identity
            counts["same"] += 1
            continue
        if target.exists():
            if target.is_file() and _same(file, target):
                file.unlink()
                counts["same"] += 1
            else:
                counts["kept"] += 1
            continue
        _move_file(file, target)
        counts["moved"] += 1
    stop = old.parent.parent if old.parent.name == DATA_DIRECTORY else old.parent
    _remove_empty(old, stop)
    message = f"已把 {old} 的文件移入目录树 {new}：移入 {counts['moved']}，相同已去重 {counts['same']}"
    if counts["kept"]:
        message += f"；{counts['kept']} 个文件与目录树里的不同，保留在原位置，请核对"
    log(message)
    return counts


def move_tree(old, new):
    """Move a whole folder that an earlier version kept outside the site tree (e.g. in the Windows profile) to
    ``new``: rename on the same drive, otherwise copy, verify every file and only then delete the old folder.
    ``new`` must not exist yet."""
    old, new = Path(old), Path(new)
    if new.exists():
        raise FileExistsError(new)
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(old, new)
        return new
    except OSError:
        pass
    tmp = new.with_name(new.name + ".adopt-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(old, tmp)
    for file in (p for p in old.rglob("*") if p.is_file()):
        if not _same(file, tmp / file.relative_to(old)):
            shutil.rmtree(tmp, ignore_errors=True)
            raise OSError(f"复制核对失败：{file}")
    os.replace(tmp, new)
    shutil.rmtree(old, ignore_errors=True)
    return new


def adopt_all(pairs, log=lambda message: None):
    """Adopt every (old, new) pair; one failing folder does not stop the others or the download."""
    for old, new in pairs:
        try:
            adopt_folder(old, new, log)
        except OSError as exc:
            log(f"移入目录树未完成（{old}）：{exc}；下一轮继续")
