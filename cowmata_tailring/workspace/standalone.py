"""Explicit single-record sessions: no project writes to the source folders."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from .annotation_store import dated_path, work_document
from .catalog import EXCLUDE_DIRS, VIDEO_SUFFIXES, Catalog, previous_video_files
from .dataset_access import DatasetLease
from .resource_layout import resource_context
from .storage import read_json


def source_context(raw):
    raw = Path(raw).resolve(strict=True)
    for parent in raw.parents:
        if parent.parent.name == "Motion":
            from datetime import date

            try:
                date.fromisoformat(parent.name)
            except ValueError:
                continue
            return parent.parent.parent, parent.name
    from cowmata_tailring.annotation.data import load_motion_json

    from .resource_layout import day_at

    return raw.parent, day_at(load_motion_json(raw).epoch_at(0))


class StandaloneCatalog(Catalog):
    standalone = True

    def __init__(self, raw, *, day, meta_path, video=None, video_directory=None, **kwargs):
        self.raw = Path(raw).resolve(strict=True)
        root = resource_context(self.raw.parent)
        self.only_video = Path(video).resolve(strict=True) if video else None
        self.video_directory = (
            Path(video_directory).resolve(strict=True) if video_directory else root / "Video" / day
        )
        self.external_sources = {}
        self.virtual_directories = {}
        self.origin_registries = {}
        super().__init__(root, meta_path=meta_path, **kwargs)
        self.day = day
        self.dated_annotations = False
        self.raw_relative = self.raw.relative_to(self.root).as_posix()
        self._extra_day_paths.add(self.raw_relative)
        self.video_lease = (
            DatasetLease([self.only_video or self.video_directory])
            if self.only_video or self.video_directory.is_dir()
            else None
        )

    def close(self):
        super().close()
        lease = getattr(self, "video_lease", None)
        if lease:
            lease.close()

    def source_path(self, relative):
        if relative in self.external_sources:
            return self.external_sources[relative]
        return super().source_path(relative)

    def rows(self, **kwargs):
        return [
            {
                **r,
                **(
                    {"external_source": str(self.source_path(r["path"]))}
                    if r["kind"] == "video"
                    else {}
                ),
            }
            for r in super().rows(**kwargs)
        ]

    def work_path(self, asset_id, *, for_write=False):
        if len(asset_id) != 64 or any(c not in "0123456789abcdef" for c in asset_id):
            raise ValueError("非法素材标识")
        return self.meta / "session.json"

    def read_work(self, asset_id):
        checkpoint = self.meta / "session.json"
        if not checkpoint.is_file():
            for path in (
                dated_path(self.root / "标注工程", self.raw_relative),
                self.root / "标注工程/annotations" / (asset_id + ".json"),
            ):
                if path and path.is_file():
                    data = read_json(path, {})
                    if (data.get("source", {}).get("asset_id") or data.get("asset_id")) == asset_id:
                        self.loaded_label_path = path
                        return data
        return read_json(checkpoint, None)

    def default_label_path(self):
        return dated_path(self.root / "标注工程", self.raw_relative) or self.raw.with_suffix(
            ".标注.json"
        )

    def saved_work_assets(self):
        value = read_json(self.meta / "session.json", {})
        return {value["asset_id"]} if value.get("asset_id") else set()

    def in_scope(self, relative):
        return (
            relative == self.raw_relative
            or relative.startswith("Video/" + self.day + "/")
            or relative in self._extra_day_paths
        )

    def walk_scope(self, error):
        yield str(self.raw.parent), [], [self.raw.name]
        if self.only_video:
            relative = "Video/" + self.day + "/视角01/" + self.only_video.name
            self.external_sources[relative] = self.only_video
            virtual = self.root / Path(relative).parent
            self.virtual_directories[str(virtual)] = str(self.only_video.parent)
            yield str(virtual), [], [self.only_video.name]
            return
        if not self.video_directory.is_dir():
            return
        for folder, dirs, files in os.walk(self.video_directory, onerror=error, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if d not in EXCLUDE_DIRS
                and not (Path(folder) / d).is_symlink()
                and not getattr(Path(folder) / d, "is_junction", lambda: False)()
            ]
            relative_dir = Path(folder).relative_to(self.video_directory)
            if str(relative_dir) == ".":
                relative_dir = Path("视角01")
            virtual = self.root / "Video" / self.day / relative_dir
            self.virtual_directories[str(virtual)] = folder
            chosen = []
            for name in files:
                actual = Path(folder) / name
                if actual.suffix.lower() in VIDEO_SUFFIXES and not actual.is_symlink():
                    self.external_sources[(virtual / name).relative_to(self.root).as_posix()] = (
                        actual.resolve()
                    )
                    chosen.append(name)
            yield str(virtual), [], chosen
        if self.video_directory.name == self.day and self.video_directory.parent.name == "Video":
            for actual in previous_video_files(self.video_directory.parent, self.day):
                relative = actual.relative_to(self.video_directory.parent.parent).as_posix()
                self.external_sources[relative] = actual
                self._extra_day_paths.add(relative)
                virtual = self.root / Path(relative).parent
                self.virtual_directories[str(virtual)] = str(actual.parent)
                yield str(virtual), [], [actual.name]

    def archived_record(self, relative):
        actual = self.source_path(relative)
        origin = resource_context(actual.parent)
        if origin not in self.origin_registries:
            self.origin_registries[origin] = {
                r["path"]: r
                for r in read_json(origin / "资源索引.json", {}).get("records", [])
                if r.get("kind") == "video"
            }
        return self.origin_registries[origin].get(actual.relative_to(origin).as_posix(), {})

    def index_one(self, relative, inspect, **kwargs):
        result = super().index_one(relative, inspect, **kwargs)
        if (
            not result
            or result.get("state") not in {"ready", "review"}
            or self.source_path(relative).suffix.lower() not in VIDEO_SUFFIXES
            or not result.get("asset_id")
        ):
            return result
        from .clocks import manual_video_metadata
        from .demand import camera_folder
        from .storage import atomic_json

        asset = result["asset_id"]
        metadata = result["metadata"]
        local = self.meta / "video_corrections" / (asset + ".json")
        if not local.exists():
            for parent in self.source_path(relative).parents:
                value = read_json(parent / "标注工程/video_corrections" / (asset + ".json"), {})
                if value.get("asset_id") == asset and value.get("readings"):
                    metadata = manual_video_metadata(metadata, value["readings"])
                    atomic_json(local, {**value, "camera": camera_folder(relative)})
                    break
        metadata = {**metadata, "camera": camera_folder(relative)}
        self.update_metadata(asset, metadata)
        return {
            **result,
            "metadata": metadata,
            "state": "review" if metadata.get("needs_review") else "ready",
        }


def standalone_document(catalog, work, motion, settings, rows):
    doc = work_document(catalog, work, motion, settings)
    doc["video"]["archive"] = {}
    doc["video"]["rows"] = [
        {**copy.deepcopy(row), "external_source": str(catalog.source_path(row["path"]))}
        for row in rows
        if row["kind"] == "video" and row.get("asset_id") and row["state"] in {"ready", "review"}
    ]
    return doc


def prepare_evidence(catalog, document):
    from .evidence import read_image, write_blob

    roots = [catalog.meta, catalog.root / "标注工程"]
    if getattr(catalog, "loaded_label_path", None):
        roots.append(catalog.loaded_label_path.parent)
    for event in document["work"]["project"]["events"]:
        for item in event.get("screenshots", {}).get("items", []):
            if item.get("status") != "captured":
                continue
            for root in roots:
                try:
                    payload = read_image(root, item)
                except (OSError, ValueError):
                    continue
                write_blob(catalog.meta, item, payload)
                break
