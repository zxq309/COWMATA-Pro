"""Explicit farm layout: category sensor data and shared dated recordings."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from .storage import atomic_json

MARKER = ".cowmata-farm.json"
SCHEMA = "cowmata-farm-v1"
RECORDINGS = "录像"
COLLABORATION = "科牧特_协作标注"
CATEGORY_PATHS = ("产犊", "发情", "怀孕/孕早期", "怀孕/孕中期", "怀孕/孕晚期", "正常", "疫病", "待核对", "未分类")


def farm_identity(root):
    marker = Path(root) / MARKER
    if not marker.is_file():
        return None
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("schema") != SCHEMA or value.get("recordings") != RECORDINGS:
        raise ValueError("牧场目录版本不支持，请先核对目录")
    UUID(value["farm_id"])
    return value


def shared_farm(path):
    path = Path(path).resolve()
    for directory in (path, *path.parents):
        if farm_identity(directory):
            return directory
    return None


def initialize_farm(root, *, farm_id=None):
    root = Path(root).resolve()
    existing = farm_identity(root)
    if existing:
        return existing
    if any(any((root / category / "Video").rglob("*.*")) for category in CATEGORY_PATHS):
        raise ValueError("存在旧版分类录像，请先完成共享录像迁移")
    root.mkdir(parents=True, exist_ok=True)
    identity = dict(schema=SCHEMA, farm_id=str(UUID(farm_id)) if farm_id else str(uuid4()), recordings=RECORDINGS)
    for name in (RECORDINGS, COLLABORATION):
        (root / name).mkdir(exist_ok=True)
    atomic_json(root / MARKER, identity)
    return identity


def video_root(scope):
    farm = shared_farm(scope)
    return farm / RECORDINGS if farm else Path(scope) / "Video"


def category_scope(path):
    path = Path(path).resolve()
    farm = shared_farm(path)
    if farm:
        for directory in (path, *path.parents):
            if directory == farm:
                break
            if directory.relative_to(farm).as_posix() in CATEGORY_PATHS:
                return directory
    return None


def storage_root(scope):
    return shared_farm(scope) or Path(scope)


def adapt_import_plan(plan):
    scope = Path(plan.get("category_scope") or plan["target"])
    farm = shared_farm(scope)
    if farm is None or plan.get("category_scope"):
        return plan
    prefix = scope.relative_to(farm).as_posix()
    references = []
    for row in plan.get("reference_records", []):
        path = row["path"]
        if not path.startswith(prefix + "/"):
            path = prefix + "/" + path
        references.append({**row, "path": path})
    return {**plan, "target": str(farm), "category_scope": str(scope), "reference_records": references}
