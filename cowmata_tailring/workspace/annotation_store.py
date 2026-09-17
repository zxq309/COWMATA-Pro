"""One lightweight working annotation mirrors each original modality path."""

from __future__ import annotations

import copy
import re
from pathlib import Path

FORMAT = 'cowmata-annotation'
LAYOUT = "dated-annotations-v1"


def dated_path(meta, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("标注关联路径越界")
    if (
        len(relative.parts) < 3
        or relative.parts[0] not in {"Motion", "PPG"}
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[1])
    ):
        return None
    return Path(meta) / relative.with_suffix(".标注.json")


def work_document(catalog, work, motion, settings):
    video = copy.deepcopy(work.history_video)
    video.update(
        camera_maps=copy.deepcopy(settings.get("camera_maps", video.get("camera_maps", {}))),
        camera_overrides=copy.deepcopy(
            settings.get("camera_overrides", video.get("camera_overrides", {}))
        ),
        selected_cameras=list(settings.get("selected_cameras", video.get("selected_cameras", []))),
    )
    if settings.get("video_archive"):
        video["archive"] = copy.deepcopy(settings["video_archive"])
    relative = motion.source_path.resolve().relative_to(catalog.root).as_posix()
    return dict(
        format=FORMAT,
        version=1,
        coordinates="parent_imu_ms",
        **work.category_fields(),
        **work.identity_fields(),
        work=copy.deepcopy(work.to_dict()),
        source=dict(
            asset_id=work.asset_id,
            path=relative,
            project_root_hint=str(catalog.root),
            acc_scale=motion.acc_scale,
            capture_timing=motion.capture_timing(),
        ),
        view=dict(start_ms=0, end_ms=motion.duration_ms),
        video=video,
        embedded_imu=None,
    )


def dated_documents(root, day=None):
    meta = Path(root) / "标注工程"
    for modality in ("Motion", "PPG"):
        directory = meta / modality
        if day:
            directory /= day
        if directory.is_dir():
            yield from sorted(directory.rglob("*.标注.json"))


def updated_work(path,work):
    """Preserve a canonical document's source and clock links during batch edits."""
    from .storage import read_json
    previous=read_json(path,{})
    if previous.get('format')==FORMAT:
        return {**previous,**work.category_fields(),**work.identity_fields(),'work':work.to_dict()}
    return work.to_dict()
