"""Annotation domain model and file/export helpers.

The desktop application deliberately keeps this module free of Qt and media
dependencies.  Times are milliseconds relative to the IMU recording unless a
function explicitly says otherwise.

The JSON representation is compatible with version 4 projects produced by the
standalone HTML annotator:

    {
      "_type": "bovine-annotation-project",
      "version": 4,
      "labels": [...],
      "events": [...]
    }

Unknown JSON fields are preserved during a load/save round trip.  The project
file remains v4; the annotation protocol is stored separately as ``v4`` in
the project's ``protocol`` field.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import csv
import hashlib
import io
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar

PROJECT_TYPE = "bovine-annotation-project"
PROJECT_VERSION = 4
REQUIRED_POINT_LABEL_CODES = {
    "STRAINING_ONSET",
    "AMNIOTIC_SAC_FIRST_VISIBLE",
    "FETAL_PART_FIRST_VISIBLE",
    "CALF_FULLY_EXPELLED",
    "FETAL_MEMBRANES_FULLY_EXPELLED",
    "SYNC_ANCHOR",
}
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

EVIDENCE_NAMES = {
    "both": "两者",
    "video": "仅视频",
    "curve": "仅曲线",
}
class AnnotationError(Exception):
    """Base class for annotation-core errors."""


class ProjectFormatError(AnnotationError, ValueError):
    """Raised when a project file cannot be interpreted safely."""


class IRRSourceMismatchError(AnnotationError, ValueError):
    """Raised when IRR input projects are not from the same recording."""


class IRRConfigurationError(AnnotationError, ValueError):
    """Raised when IRR settings or ethograms are incompatible."""


def _finite_float(value: Any, field_name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ProjectFormatError(f"{field_name} 不能是布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProjectFormatError(f"{field_name} 不是有效数字: {value!r}") from exc
    if not math.isfinite(number):
        raise ProjectFormatError(f"{field_name} 必须是有限数字")
    return number


def _integer(value: Any, field_name: str, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ProjectFormatError(f"{field_name} 不能是布尔值")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectFormatError(f"{field_name} 不是有效整数: {value!r}") from exc
    return result


def _first(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _extras(data: Mapping[str, Any], consumed: Iterable[str]) -> dict[str, Any]:
    keys = set(consumed)
    return {str(k): copy.deepcopy(v) for k, v in data.items() if k not in keys}


def _normalise_source(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if not source:
        return {}
    result = dict(copy.deepcopy(source))
    aliases = {
        "create_time": "createTime",
        "data_version": "dataVersion",
        "acc_scale": "accScale",
        "duration_ms": "durationMs",
        "session_id": "name",
    }
    for old, new in aliases.items():
        if new not in result and old in result:
            result[new] = result.pop(old)
    return result


def _normalise_align(align: Mapping[str, Any] | None) -> dict[str, Any]:
    if not align:
        return {}
    result = dict(copy.deepcopy(align))
    aliases = {
        "offset_ms": "offsetMs",
        "video_start_wall": "videoStartWall",
        "video_start_wall_ms": "videoStartWall",
    }
    for old, new in aliases.items():
        if new not in result and old in result:
            result[new] = result.pop(old)
    return result


@dataclass(slots=True)
class Label:
    """One ethogram label."""

    name: str
    en: str = ""
    key: str = ""
    color: str = "#79b6ff"
    type: str = "interval"
    cat: str = ""
    minDur: float = 0.0
    overlap: bool = True
    definition: str = ""
    extras: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def min_duration_s(self) -> float:
        return self.minDur

    @min_duration_s.setter
    def min_duration_s(self, value: float) -> None:
        self.minDur = float(value)

    @property
    def is_point(self) -> bool:
        return self.type == "point"

    @property
    def is_interval(self) -> bool:
        return self.type == "interval"

    @property
    def is_state(self) -> bool:
        return self.layer == "body_state" or self.cat == "状态"

    @property
    def code(self) -> str:
        return str(self.extras.get("code", self.name))

    @property
    def layer(self) -> str:
        return str(
            self.extras.get(
                "layer", "body_state" if self.cat == "状态" else "objective_event"
            )
        )

    @property
    def specialty(self) -> bool:
        return bool(self.extras.get("specialty", False))

    @property
    def trainable(self) -> bool:
        return bool(self.extras.get("trainable", True))

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extras)
        result.update(
            {
                "name": self.name,
                "en": self.en,
                "key": self.key,
                "color": self.color,
                "type": self.type,
                "cat": self.cat,
                "minDur": self.minDur,
                "overlap": self.overlap,
                "def": self.definition,
                "code": self.code,
                "layer": self.layer,
                "specialty": self.specialty,
                "trainable": self.trainable,
            }
        )
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | str) -> Label:
        if isinstance(raw, str):
            return cls(name=raw)
        if not isinstance(raw, Mapping):
            raise ProjectFormatError("标签必须是对象")
        consumed = {
            "name", "label", "en", "english", "key", "shortcut", "color",
            "type", "kind", "cat", "category", "minDur", "min_duration_s",
            "minDuration", "min_duration_ms", "overlap", "allow_overlap",
            "def", "definition", "description", "code", "layer",
            "specialty", "trainable",
        }
        name = str(_first(raw, "name", "label", default="")).strip()
        if not name:
            raise ProjectFormatError("标签名称不能为空")
        label_type = str(_first(raw, "type", "kind", default="interval")).lower()
        if label_type in {"event", "state", "duration"}:
            label_type = "interval"
        elif label_type in {"moment", "instant"}:
            label_type = "point"
        min_duration = _first(
            raw, "minDur", "min_duration_s", "minDuration", default=None
        )
        if min_duration is None and "min_duration_ms" in raw:
            min_duration = _finite_float(
                raw["min_duration_ms"], "label.min_duration_ms"
            ) / 1000.0
        min_duration = _finite_float(
            0.0 if min_duration is None else min_duration, "label.minDur"
        )
        key = str(_first(raw, "key", "shortcut", default=""))
        extras = _extras(raw, consumed)
        for field_name in ("code", "layer", "specialty", "trainable"):
            if field_name in raw:
                extras[field_name] = copy.deepcopy(raw[field_name])
        return cls(
            name=name,
            en=str(_first(raw, "en", "english", default="")),
            key=key[:1],
            color=str(raw.get("color", "#79b6ff")),
            type=label_type,
            cat=str(_first(raw, "cat", "category", default="")),
            minDur=float(min_duration),
            overlap=bool(_first(raw, "overlap", "allow_overlap", default=True)),
            definition=str(
                _first(raw, "def", "definition", "description", default="")
            ),
            extras=extras,
        )


@dataclass(slots=True)
class Event:
    """A point or interval annotation."""

    id: int
    li: int
    t0: float
    t1: float | None = None
    note: str = ""
    ev: str = "both"
    ctx: str = ""
    extras: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def start_ms(self) -> float:
        return self.t0

    @start_ms.setter
    def start_ms(self, value: float) -> None:
        self.t0 = float(value)

    @property
    def end_ms(self) -> float | None:
        return self.t1

    @end_ms.setter
    def end_ms(self, value: float | None) -> None:
        self.t1 = None if value is None else float(value)

    @property
    def label_index(self) -> int:
        return self.li

    @label_index.setter
    def label_index(self, value: int) -> None:
        self.li = int(value)

    @property
    def evidence(self) -> str:
        return self.ev

    @evidence.setter
    def evidence(self, value: str) -> None:
        self.ev = value

    @property
    def context(self) -> str:
        return self.ctx

    @context.setter
    def context(self, value: str) -> None:
        self.ctx = value

    @property
    def duration_ms(self) -> float | None:
        return None if self.t1 is None else self.t1 - self.t0

    @property
    def is_point(self) -> bool:
        return self.t1 is None

    @property
    def reviewed_range(self) -> dict[str, float | None]:
        return {
            "start": float(self.t0),
            "end": None if self.t1 is None else float(self.t1),
        }

    def normalise_bounds(self) -> None:
        if self.t1 is not None and self.t1 < self.t0:
            self.t0, self.t1 = self.t1, self.t0

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extras)
        result.update(
            {
                "id": self.id,
                "li": self.li,
                "t0": self.t0,
                "t1": self.t1,
                "note": self.note,
                "ev": self.ev,
                "ctx": self.ctx,
                "reviewed_range": self.reviewed_range,
            }
        )
        return result

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        fallback_id: int,
        labels: Sequence[Label],
    ) -> Event:
        if not isinstance(raw, Mapping):
            raise ProjectFormatError("事件必须是对象")
        consumed = {
            "id", "event_id", "li", "labelIndex", "label_index", "label",
            "label_name", "t0", "start", "start_ms", "t_start_rel_ms",
            "t1", "end", "end_ms", "t_end_rel_ms", "note", "comment",
            "ev", "evidence", "ctx", "context",
            "reviewed_range",
        }
        label_ref = _first(raw, "li", "labelIndex", "label_index", default=None)
        if label_ref is None:
            label_ref = _first(raw, "label", "label_name", default=None)
            if isinstance(label_ref, str):
                label_ref = next(
                    (i for i, label in enumerate(labels) if label.name == label_ref),
                    -1,
                )
        label_index = _integer(label_ref, "event.li", default=-1)
        reviewed = raw.get("reviewed_range")
        if reviewed is not None and not isinstance(reviewed, Mapping):
            raise ProjectFormatError("event.reviewed_range 必须是对象")
        start = _first(raw, "t0", "start", "start_ms", "t_start_rel_ms", default=None)
        if start is None and isinstance(reviewed, Mapping):
            start = _first(reviewed, "start", "t0", default=None)
        if start is None:
            raise ProjectFormatError("事件缺少开始时间")
        end_present = any(
            key in raw for key in ("t1", "end", "end_ms", "t_end_rel_ms")
        )
        end = _first(raw, "t1", "end", "end_ms", "t_end_rel_ms", default=None)
        if not end_present and isinstance(reviewed, Mapping) and "end" in reviewed:
            end_present = True
            end = reviewed.get("end")
        return cls(
            id=_integer(_first(raw, "id", "event_id", default=fallback_id), "event.id"),
            li=label_index,
            t0=float(_finite_float(start, "event.t0")),
            t1=(
                float(_finite_float(end, "event.t1"))
                if end_present and end not in (None, "")
                else None
            ),
            note=str(_first(raw, "note", "comment", default="")),
            ev=str(_first(raw, "ev", "evidence", default="both")),
            ctx=str(_first(raw, "ctx", "context", default="")),
            extras=_extras(raw, consumed),
        )


@dataclass(slots=True)
class Project:
    """Complete annotation project."""

    annotator: str = ""
    protocol: str = ""
    cow_id: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    align: dict[str, Any] = field(default_factory=dict)
    videoName: str = ""
    labels: list[Label] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    version: int = PROJECT_VERSION
    extras: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def video_name(self) -> str:
        return self.videoName

    @video_name.setter
    def video_name(self, value: str) -> None:
        self.videoName = value

    @property
    def tag_name(self) -> str:
        return str(self.source.get("name", ""))

    @tag_name.setter
    def tag_name(self, value: str) -> None:
        self.source["name"] = value

    @property
    def next_event_id(self) -> int:
        return max((event.id for event in self.events), default=0) + 1

    @property
    def duration_ms(self) -> float | None:
        for key in ("durationMs", "tspan", "tspanMs"):
            value = self.source.get(key)
            if value is not None:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number) and number >= 0:
                    return number
        return None

    def clone(self) -> Project:
        return Project.from_dict(self.to_dict())

    def sort_events(self) -> None:
        self.events.sort(key=lambda event: (event.t0, event.id))

    def add_event(
        self,
        label_index: int,
        start_ms: float,
        end_ms: float | None = None,
        *,
        note: str = "",
        evidence: str = "both",
        context: str = "",
        event_id: int | None = None,
        normalise: bool = True,
    ) -> Event:
        event = Event(
            id=self.next_event_id if event_id is None else int(event_id),
            li=int(label_index),
            t0=float(start_ms),
            t1=None if end_ms is None else float(end_ms),
            note=note,
            ev=evidence,
            ctx=context,
        )
        if normalise:
            event.normalise_bounds()
        self.events.append(event)
        self.sort_events()
        return event

    def remove_event(self, event_id: int) -> Event | None:
        for index, event in enumerate(self.events):
            if event.id == event_id:
                return self.events.pop(index)
        return None

    def event_by_id(self, event_id: int) -> Event | None:
        return next((event for event in self.events if event.id == event_id), None)

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extras)
        events: list[dict[str, Any]] = []
        for event in self.events:
            value = event.to_dict()
            value["data_id"] = str(self.source.get("name", ""))
            value["video_id"] = self.videoName
            value["cow_id"] = self.cow_id
            value["annotator"] = self.annotator
            value["protocol"] = self.protocol
            if 0 <= event.li < len(self.labels):
                label = self.labels[event.li]
                value["label_code"] = label.code
                value["layer"] = label.layer
            events.append(value)
        result.update(
            {
                "_type": PROJECT_TYPE,
                "version": PROJECT_VERSION,
                "annotator": self.annotator,
                "protocol": self.protocol,
                "cow_id": self.cow_id,
                "source": copy.deepcopy(self.source),
                "align": copy.deepcopy(self.align),
                "videoName": self.videoName,
                "labels": [label.to_dict() for label in self.labels],
                "events": events,
            }
        )
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Project:
        if not isinstance(raw, Mapping):
            raise ProjectFormatError("工程文件顶层必须是 JSON 对象")
        project_type = raw.get("_type")
        if project_type not in (None, "", PROJECT_TYPE):
            raise ProjectFormatError(f"不是本工具的工程文件: {project_type!r}")
        version = _integer(raw.get("version"), "project.version", default=1)
        if version < 1:
            raise ProjectFormatError(f"不支持的工程版本: {version}")
        if version > PROJECT_VERSION:
            raise ProjectFormatError(
                f"工程版本 {version} 高于本工具支持的 v{PROJECT_VERSION}"
            )

        label_values = raw.get("labels", [])
        if not isinstance(label_values, Sequence) or isinstance(
            label_values, (str, bytes, bytearray)
        ):
            raise ProjectFormatError("labels 必须是数组")
        labels = [Label.from_dict(item) for item in label_values]

        event_values = raw.get("events", raw.get("annotations", []))
        if not isinstance(event_values, Sequence) or isinstance(
            event_values, (str, bytes, bytearray)
        ):
            raise ProjectFormatError("events 必须是数组")
        events = [
            Event.from_dict(item, fallback_id=index + 1, labels=labels)
            for index, item in enumerate(event_values)
        ]

        # Old files occasionally repeated IDs or omitted them.  Preserve the
        # first valid ID and deterministically repair later duplicates.
        used_ids: set[int] = set()
        next_id = max((event.id for event in events if event.id > 0), default=0) + 1
        for event in events:
            if event.id <= 0 or event.id in used_ids:
                while next_id in used_ids:
                    next_id += 1
                event.id = next_id
                next_id += 1
            used_ids.add(event.id)
        events.sort(key=lambda event: (event.t0, event.id))

        consumed = {
            "_type", "version", "annotator", "protocol", "cow_id", "cowId",
            "source", "align",
            "videoName", "video_name", "labels", "events", "annotations",
        }
        return cls(
            annotator=str(raw.get("annotator", "")),
            protocol=str(raw.get("protocol", "")),
            cow_id=str(_first(raw, "cow_id", "cowId", default="")),
            source=_normalise_source(
                raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
            ),
            align=_normalise_align(
                raw.get("align") if isinstance(raw.get("align"), Mapping) else {}
            ),
            videoName=str(_first(raw, "videoName", "video_name", default="")),
            labels=labels,
            events=events,
            version=PROJECT_VERSION,
            extras=_extras(raw, consumed),
        )


def default_labels() -> list[Label]:
    """Return a fresh copy of the current annotation-protocol ethogram."""

    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    return [Label.from_dict(copy.deepcopy(value)) for value in DEFAULT_LABELS]


def new_project(**kwargs: Any) -> Project:
    """Create a project with the standard ethogram."""

    from cowmata_tailring.annotation.defaults import DEFAULT_PROTOCOL

    kwargs.setdefault("labels", default_labels())
    kwargs.setdefault("protocol", DEFAULT_PROTOCOL)
    return Project(**kwargs)


def project_to_json(project: Project, *, indent: int | None = 2) -> str:
    return json.dumps(
        project.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
    )


def project_from_json(text: str | bytes | bytearray) -> Project:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig")
    try:
        raw = json.loads(text.lstrip("\ufeff"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectFormatError(f"工程 JSON 解析失败: {exc}") from exc
    return Project.from_dict(raw)


def load_project(path: str | os.PathLike[str]) -> Project:
    project_path = Path(path)
    try:
        text = project_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ProjectFormatError(f"无法读取工程文件 {project_path}: {exc}") from exc
    return project_from_json(text)


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise
    return path


def save_project(
    project: Project,
    path: str | os.PathLike[str],
    *,
    indent: int | None = 2,
) -> Path:
    """Atomically save a v4 UTF-8 project."""

    return _atomic_write_text(Path(path), project_to_json(project, indent=indent) + "\n")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    event_id: int | None = None
    label_index: int | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "event_id": self.event_id,
            "label_index": self.label_index,
        }


def validate_project(
    project: Project,
    *,
    duration_ms: float | None = None,
    require_alignment: bool = True,
    require_cow_id: bool = False,
    check_state_coverage: bool = True,
    gap_warning_ms: float = 2000.0,
) -> list[ValidationIssue]:
    """Run structural and annotation-quality checks.

    Structural problems are ``error``; research-protocol completeness findings
    such as too-short events or state gaps are ``warning``.
    """

    issues: list[ValidationIssue] = []
    if duration_ms is None:
        duration_ms = project.duration_ms
    if duration_ms is not None and (
        not math.isfinite(float(duration_ms)) or float(duration_ms) < 0
    ):
        issues.append(
            ValidationIssue("error", "invalid_duration", "数据总时长不是有效非负数")
        )
        duration_ms = None

    if require_cow_id and not project.cow_id.strip():
        issues.append(
            ValidationIssue(
                "error", "missing_cow_id", "进入训练数据导出前必须填写真实 cow_id"
            )
        )

    names: dict[str, int] = {}
    shortcuts: dict[str, int] = {}
    for index, label in enumerate(project.labels):
        name = label.name.strip()
        if not name:
            issues.append(
                ValidationIssue(
                    "error", "empty_label_name", f"标签 #{index + 1} 名称为空",
                    label_index=index,
                )
            )
        elif name in names:
            issues.append(
                ValidationIssue(
                    "error", "duplicate_label_name",
                    f"标签名称“{name}”重复，IRR 无法可靠匹配",
                    label_index=index,
                )
            )
        else:
            names[name] = index
        if label.key:
            key = label.key.casefold()
            if key in shortcuts:
                issues.append(
                    ValidationIssue(
                        "warning", "duplicate_shortcut",
                        f"快捷键“{label.key}”被多个标签使用",
                        label_index=index,
                    )
                )
            else:
                shortcuts[key] = index
        if label.type not in {"interval", "point"}:
            issues.append(
                ValidationIssue(
                    "error", "invalid_label_type",
                    f"标签“{label.name}”类型必须是 interval 或 point",
                    label_index=index,
                )
            )
        if not math.isfinite(label.minDur) or label.minDur < 0:
            issues.append(
                ValidationIssue(
                    "error", "invalid_min_duration",
                    f"标签“{label.name}”最短时长无效",
                    label_index=index,
                )
            )
        if label.code in REQUIRED_POINT_LABEL_CODES and not label.is_point:
            issues.append(
                ValidationIssue(
                    "error", "point_label_required",
                    f"标签“{label.name}”按协议必须是点事件",
                    label_index=index,
                )
            )

    seen_ids: set[int] = set()
    valid_state_events: list[tuple[float, float, Event, Label]] = []
    no_overlap_events: dict[int, list[Event]] = {}
    for ordinal, event in enumerate(project.events, 1):
        if event.id in seen_ids:
            issues.append(
                ValidationIssue(
                    "error", "duplicate_event_id",
                    f"事件 ID {event.id} 重复", event_id=event.id,
                )
            )
        seen_ids.add(event.id)
        if event.li < 0 or event.li >= len(project.labels):
            issues.append(
                ValidationIssue(
                    "error", "invalid_label_index",
                    f"事件 #{ordinal} 引用了不存在的标签 #{event.li}",
                    event_id=event.id, label_index=event.li,
                )
            )
            continue
        label = project.labels[event.li]
        if not math.isfinite(event.t0):
            issues.append(
                ValidationIssue(
                    "error", "invalid_start",
                    f"事件 #{ordinal} {label.name} 开始时间无效",
                    event_id=event.id, label_index=event.li,
                )
            )
            continue
        if event.t0 < 0 or (
            duration_ms is not None and event.t0 > float(duration_ms)
        ):
            issues.append(
                ValidationIssue(
                    "error", "start_out_of_range",
                    f"事件 #{ordinal} {label.name} 开始时间超出数据范围",
                    event_id=event.id, label_index=event.li,
                )
            )

        if label.is_point:
            if event.t1 is not None:
                issues.append(
                    ValidationIssue(
                        "warning", "point_has_end",
                        f"点事件 #{ordinal} {label.name} 含结束时间；导出时按点处理",
                        event_id=event.id, label_index=event.li,
                    )
                )
            continue

        if event.t1 is None:
            issues.append(
                ValidationIssue(
                    "error", "interval_missing_end",
                    f"区间事件 #{ordinal} {label.name} 缺少结束时间",
                    event_id=event.id, label_index=event.li,
                )
            )
            continue
        if not math.isfinite(event.t1):
            issues.append(
                ValidationIssue(
                    "error", "invalid_end",
                    f"事件 #{ordinal} {label.name} 结束时间无效",
                    event_id=event.id, label_index=event.li,
                )
            )
            continue
        if event.t1 <= event.t0:
            issues.append(
                ValidationIssue(
                    "error", "end_not_after_start",
                    f"事件 #{ordinal} {label.name} 的结束时间必须晚于开始时间",
                    event_id=event.id, label_index=event.li,
                )
            )
            continue
        if duration_ms is not None and event.t1 > float(duration_ms):
            issues.append(
                ValidationIssue(
                    "error", "end_out_of_range",
                    f"事件 #{ordinal} {label.name} 结束时间超出数据范围",
                    event_id=event.id, label_index=event.li,
                )
            )
        if label.minDur and event.t1 - event.t0 < label.minDur * 1000:
            issues.append(
                ValidationIssue(
                    "warning", "below_min_duration",
                    f"事件 #{ordinal} {label.name} 短于最短时长 {label.minDur:g}s",
                    event_id=event.id, label_index=event.li,
                )
            )
        if label.is_state:
            valid_state_events.append((event.t0, event.t1, event, label))
        if not label.overlap:
            no_overlap_events.setdefault(event.li, []).append(event)

    # Any two states are mutually exclusive, even when they are different
    # labels.  Sweep all valid state intervals in temporal order.
    state_sorted = sorted(valid_state_events, key=lambda item: (item[0], item[1]))
    for index, current in enumerate(state_sorted):
        start, end, event, label = current
        for other in state_sorted[index + 1 :]:
            if other[0] >= end - 1:
                break
            if other[1] > start + 1:
                issues.append(
                    ValidationIssue(
                        "error", "exclusive_state_overlap",
                        f"互斥状态重叠：{label.name} × {other[3].name} @ {other[0]:.0f}ms",
                        event_id=other[2].id, label_index=other[2].li,
                    )
                )

    # Non-overlapping custom labels also receive a same-label check.
    for label_index, events in no_overlap_events.items():
        events = sorted(
            (event for event in events if event.t1 is not None),
            key=lambda event: (event.t0, event.t1 or event.t0),
        )
        previous: Event | None = None
        for event in events:
            if previous is not None and event.t0 < (previous.t1 or previous.t0) - 1:
                if not project.labels[label_index].is_state:
                    issues.append(
                        ValidationIssue(
                            "error", "forbidden_label_overlap",
                            f"标签“{project.labels[label_index].name}”不允许重叠",
                            event_id=event.id, label_index=label_index,
                        )
                    )
            if previous is None or (event.t1 or event.t0) > (
                previous.t1 or previous.t0
            ):
                previous = event

    if check_state_coverage and duration_ms is not None and state_sorted:
        merged: list[list[float]] = []
        for start, end, _, _ in state_sorted:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        previous_end = 0.0
        for start, end in merged:
            if start - previous_end > gap_warning_ms:
                issues.append(
                    ValidationIssue(
                        "warning", "state_coverage_gap",
                        f"状态未覆盖空隙 {previous_end:.0f}–{start:.0f}ms",
                    )
                )
            previous_end = max(previous_end, end)
        if float(duration_ms) - previous_end > gap_warning_ms:
            issues.append(
                ValidationIssue(
                    "warning", "state_coverage_gap",
                    f"状态未覆盖空隙 {previous_end:.0f}ms–末尾",
                )
            )

    if require_alignment:
        offset = project.align.get("offsetMs")
        start_wall = project.align.get("videoStartWall")
        if offset is None and start_wall is None:
            issues.append(
                ValidationIssue(
                    "warning", "missing_alignment",
                    "视频未加载或未对齐（同步偏移缺失）",
                )
            )
    return issues


def has_validation_errors(issues: Iterable[ValidationIssue]) -> bool:
    return any(issue.is_error for issue in issues)


def _csv_text(rows: Iterable[Sequence[Any]], *, bom: bool = True) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows(rows)
    text = output.getvalue()
    return ("\ufeff" + text) if bom else text


def _format_wall_ms(value_ms: float | int | None) -> str:
    if value_ms is None:
        return ""
    try:
        number = float(value_ms)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    value = datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).astimezone(
        BEIJING_TZ
    )
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"


def _source_json_metadata(
    source: Mapping[str, Any], supplied: Mapping[str, Any]
) -> dict[str, Any]:
    """Return stable identity fields for the source IMU JSON.

    The path is intentionally stored alongside the filename.  A filename is
    usually enough for the current export layout, but it is not unique once
    several device/date folders are reviewed together.  The partial digest is
    inexpensive for large recordings and catches a file being silently
    replaced without requiring the whole JSON to be read.
    """

    raw_path = supplied.get("source_json_path", source.get("path", ""))
    path_text = str(raw_path or "").strip()
    result = {
        "source_json_path": path_text,
        "source_json_name": Path(path_text).name if path_text else "",
        "source_json_size": supplied.get("source_json_size", ""),
        "source_json_mtime_ns": supplied.get("source_json_mtime_ns", ""),
        "source_json_fingerprint": supplied.get(
            "source_json_fingerprint", ""
        ),
    }
    if not path_text:
        return result

    path = Path(path_text).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        result["source_json_path"] = str(resolved)
        result["source_json_name"] = resolved.name
        result["source_json_size"] = int(stat.st_size)
        result["source_json_mtime_ns"] = int(stat.st_mtime_ns)
        if not result["source_json_fingerprint"]:
            digest = hashlib.sha256()
            chunk_size = 65_536
            digest.update(str(stat.st_size).encode("ascii"))
            with resolved.open("rb") as handle:
                digest.update(handle.read(chunk_size))
                if stat.st_size > chunk_size:
                    handle.seek(max(0, stat.st_size - chunk_size))
                    digest.update(handle.read(chunk_size))
            result["source_json_fingerprint"] = (
                "sha256-partial:" + digest.hexdigest()
            )
    except (OSError, ValueError):
        # Keep caller-supplied identity values if the source is unavailable.
        # Exporting annotations should still work when the original drive is
        # temporarily disconnected; the review UI will show the missing JSON.
        pass
    return result


def _session_id(project: Project, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if project.source.get("name"):
        return str(project.source["name"])
    device = str(project.source.get("device", ""))
    create_time = project.source.get("createTime")
    if device and create_time not in (None, ""):
        return f"{device}_{create_time}"
    return device


def build_events_csv(
    project: Project,
    *,
    session_id: str | None = None,
    relative_timestamps_ms: Sequence[float] | None = None,
) -> str:
    """Build a flat interoperability CSV from the v2 annotation project."""

    header = [
        "index", "session_id", "cow_id", "layer", "label", "code", "en",
        "type", "evidence", "annotator", "protocol",
        "t_start_rel_ms", "t_end_rel_ms",
        "reviewed_start_ms", "reviewed_end_ms", "duration_ms", "frame_start",
        "frame_end", "t_start_wall_bj", "t_end_wall_bj", "note",
    ]
    rows: list[list[Any]] = [header]
    recording_start = project.source.get("createTime")
    sid = _session_id(project, session_id)
    timestamps = relative_timestamps_ms
    for ordinal, event in enumerate(sorted(project.events, key=lambda e: e.t0), 1):
        label = project.labels[event.li] if 0 <= event.li < len(project.labels) else None
        frame_start: int | str = ""
        frame_end: int | str = ""
        if timestamps is not None:
            frame_start = bisect.bisect_left(timestamps, event.t0)
            if event.t1 is not None:
                frame_end = bisect.bisect_left(timestamps, event.t1)
        start_wall = (
            _format_wall_ms(float(recording_start) + event.t0)
            if recording_start not in (None, "")
            else ""
        )
        end_wall = (
            _format_wall_ms(float(recording_start) + event.t1)
            if recording_start not in (None, "") and event.t1 is not None
            else ""
        )
        rows.append(
            [
                ordinal,
                sid,
                project.cow_id,
                label.layer if label else "",
                label.name if label else "",
                label.code if label else "",
                label.en if label else "",
                label.type if label else "",
                EVIDENCE_NAMES.get(event.ev, event.ev),
                project.annotator,
                project.protocol,
                round(event.t0),
                "" if event.t1 is None else round(event.t1),
                round(event.reviewed_range["start"]),
                "" if event.reviewed_range["end"] is None else round(event.reviewed_range["end"]),
                "" if event.t1 is None else round(event.t1 - event.t0),
                frame_start,
                frame_end,
                start_wall,
                end_wall,
                event.note,
            ]
        )
    return _csv_text(rows)


def export_events_csv(
    project: Project,
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    relative_timestamps_ms: Sequence[float] | None = None,
) -> Path:
    return _atomic_write_text(
        Path(path),
        build_events_csv(
            project,
            session_id=session_id,
            relative_timestamps_ms=relative_timestamps_ms,
        ),
        encoding="utf-8",
    )


def build_sample_multihot_csv(
    project: Project,
    sample_times_ms: Sequence[float],
    *,
    include_non_trainable: bool = False,
) -> str:
    """Align independent annotations to the real JSON sample times."""

    times = [float(value) for value in sample_times_ms]
    if any(not math.isfinite(value) for value in times):
        raise ProjectFormatError("sample_times_ms 必须全部是有限数字")
    if any(right < left for left, right in zip(times, times[1:])):
        raise ProjectFormatError("sample_times_ms 必须按时间递增")
    labels = [
        (index, label)
        for index, label in enumerate(project.labels)
        if include_non_trainable or label.trainable
    ]
    columns = [f"{label.layer}__{label.code}" for _, label in labels]
    header = [
        "sample_index", "sample_time_ms", "cow_id", "reviewed_any",
        *columns,
    ]
    rows = [
        [
            index, round(time, 3), project.cow_id, 0,
            *([0] * len(columns)),
        ]
        for index, time in enumerate(times)
    ]
    label_columns = {
        label_index: position + 4
        for position, (label_index, _) in enumerate(labels)
    }

    def event_indices(event: Event) -> list[int]:
        if not times:
            return []
        if event.t1 is None:
            right = bisect.bisect_left(times, event.t0)
            candidates = ([right] if right < len(times) else []) + (
                [right - 1] if right > 0 else []
            )
            return [min(candidates, key=lambda i: abs(times[i] - event.t0))] if candidates else []
        first = bisect.bisect_left(times, event.t0)
        last = bisect.bisect_left(times, event.t1, first)
        return list(range(first, last))

    for event in project.events:
        column = label_columns.get(event.li)
        if column is None:
            continue
        for sample_index in event_indices(event):
            rows[sample_index][3] = 1
            rows[sample_index][column] = 1
    return _csv_text([header, *rows])


def export_sample_multihot_csv(
    project: Project,
    path: str | os.PathLike[str],
    sample_times_ms: Sequence[float],
    *,
    include_non_trainable: bool = False,
) -> Path:
    return _atomic_write_text(
        Path(path),
        build_sample_multihot_csv(
            project,
            sample_times_ms,
            include_non_trainable=include_non_trainable,
        ),
    )


def build_meta(
    project: Project,
    *,
    session_id: str | None = None,
    data_meta: Mapping[str, Any] | None = None,
    exported_at_ms: float | None = None,
) -> dict[str, Any]:
    """Build the companion ``events_meta.json`` object.

    ``data_meta`` may provide runtime-only values such as ``sampling_hz``,
    ``gap_count`` and ``resting_g``.
    """

    source = project.source
    supplied = dict(data_meta or {})
    exported_at_ms = (
        datetime.now(tz=timezone.utc).timestamp() * 1000
        if exported_at_ms is None
        else exported_at_ms
    )
    metadata = {
        "tool": "牛尾环九轴桌面标注工具",
        "project_version": PROJECT_VERSION,
        "session_id": _session_id(project, session_id),
        "cow_id": project.cow_id,
        "annotator": project.annotator,
        "protocol": project.protocol,
        "device": source.get("device", ""),
        "create_time_bj": _format_wall_ms(source.get("createTime")),
        "imu_version": source.get("dataVersion", source.get("version", "")),
        "acc_scale_divisor": source.get("accScale", ""),
        "sampling_hz": supplied.get("sampling_hz", source.get("samplingHz", "")),
        "gap_count": supplied.get("gap_count", source.get("gapCount", "")),
        "resting_g": supplied.get("resting_g", source.get("restingG", "")),
        "align_method": project.align.get("method", ""),
        "align_offset_ms": project.align.get("offsetMs", ""),
        "video_name": project.videoName,
        "exported_bj": _format_wall_ms(exported_at_ms),
    }
    # Preserve arbitrary runtime metadata so future calibration parameters can
    # be added outside the package without requiring another schema migration.
    metadata.update(supplied)
    metadata.update(_source_json_metadata(source, supplied))
    return metadata


def export_meta_json(
    project: Project,
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    data_meta: Mapping[str, Any] | None = None,
) -> Path:
    text = json.dumps(
        build_meta(project, session_id=session_id, data_meta=data_meta),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return _atomic_write_text(Path(path), text + "\n")


def build_boris_csv(
    project: Project,
    *,
    observation_id: str | None = None,
) -> str:
    """Build BORIS-style aggregate event CSV."""

    observation = observation_id or _session_id(project) or "observation"
    rows: list[list[Any]] = [
        [
            "Observation id", "Behavior", "Behavioral category",
            "Start (s)", "Stop (s)", "Duration (s)",
        ]
    ]
    for event in sorted(project.events, key=lambda item: item.t0):
        label = project.labels[event.li] if 0 <= event.li < len(project.labels) else None
        start_s = event.t0 / 1000.0
        stop_s = start_s if event.t1 is None else event.t1 / 1000.0
        rows.append(
            [
                observation,
                label.name if label else f"#{event.li}",
                "point" if label and label.is_point else "state",
                f"{start_s:.3f}",
                f"{stop_s:.3f}",
                f"{stop_s - start_s:.3f}",
            ]
        )
    return _csv_text(rows)


def export_boris_csv(
    project: Project,
    path: str | os.PathLike[str],
    *,
    observation_id: str | None = None,
) -> Path:
    return _atomic_write_text(
        Path(path), build_boris_csv(project, observation_id=observation_id)
    )


@dataclass(frozen=True, slots=True)
class IRRLabelResult:
    label: str
    type: str
    n_a: int
    n_b: int
    matched: int
    precision: float
    recall: float
    event_f1: float
    frame_kappa: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "type": self.type,
            "n_A": self.n_a,
            "n_B": self.n_b,
            "matched": self.matched,
            "precision": self.precision,
            "recall": self.recall,
            "event_F1": self.event_f1,
            "frame_kappa": self.frame_kappa,
        }


@dataclass(frozen=True, slots=True)
class IRRReport:
    rows: tuple[IRRLabelResult, ...]
    micro_precision: float
    micro_recall: float
    micro_f1: float
    macro_kappa: float
    matched_total: int
    n_a_total: int
    n_b_total: int
    duration_ms: float
    iou_threshold: float
    point_tolerance_ms: float
    frame_step_ms: float
    annotator_a: str = ""
    annotator_b: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotator_A": self.annotator_a,
            "annotator_B": self.annotator_b,
            "settings": {
                "interval_iou_threshold": self.iou_threshold,
                "point_tolerance_ms": self.point_tolerance_ms,
                "frame_step_ms": self.frame_step_ms,
                "duration_ms": self.duration_ms,
            },
            "labels": [row.to_dict() for row in self.rows],
            "overall": {
                "n_A": self.n_a_total,
                "n_B": self.n_b_total,
                "matched": self.matched_total,
                "micro_precision": self.micro_precision,
                "micro_recall": self.micro_recall,
                "micro_F1": self.micro_f1,
                "macro_kappa": self.macro_kappa,
            },
        }

    def to_csv(self) -> str:
        rows: list[list[Any]] = [
            [
                "label", "type", "n_A", "n_B", "matched", "event_F1",
                "frame_kappa",
            ]
        ]
        for row in self.rows:
            rows.append(
                [
                    row.label,
                    row.type,
                    row.n_a,
                    row.n_b,
                    row.matched,
                    f"{row.event_f1:.4f}",
                    "" if row.frame_kappa is None else f"{row.frame_kappa:.4f}",
                ]
            )
        rows.append(
            [
                "__overall__", "", self.n_a_total, self.n_b_total,
                self.matched_total, f"{self.micro_f1:.4f}",
                f"{self.macro_kappa:.4f}",
            ]
        )
        return _csv_text(rows)


@dataclass(frozen=True, slots=True)
class _NamedEvent:
    name: str
    type: str
    t0: float
    t1: float | None


def _named_events(project: Project) -> list[_NamedEvent]:
    result: list[_NamedEvent] = []
    for event in project.events:
        if 0 <= event.li < len(project.labels):
            label = project.labels[event.li]
            result.append(_NamedEvent(label.name, label.type, event.t0, event.t1))
    return result


def _span(event: _NamedEvent) -> tuple[float, float]:
    end = event.t0 if event.t1 is None else event.t1
    return min(event.t0, end), max(event.t0, end)


def _iou(a: _NamedEvent, b: _NamedEvent) -> float:
    a0, a1 = _span(a)
    b0, b1 = _span(b)
    intersection = max(0.0, min(a1, b1) - max(a0, b0))
    union = (a1 - a0) + (b1 - b0) - intersection
    if union > 0:
        return intersection / union
    return 1.0 if a0 == b0 and a1 == b1 else 0.0


def _centre(event: _NamedEvent) -> float:
    return event.t0 if event.t1 is None else (event.t0 + event.t1) / 2.0


def _mark_frames(
    frames: bytearray,
    events: Sequence[_NamedEvent],
    step_ms: float,
) -> None:
    count = len(frames)
    for event in events:
        if event.t1 is None:
            start = event.t0 - step_ms / 2.0
            end = event.t0 + step_ms / 2.0
        else:
            start, end = _span(event)
        first = max(0, int(math.floor(start / step_ms)))
        last = min(count - 1, int(math.floor(end / step_ms)))
        if last >= first:
            frames[first : last + 1] = b"\x01" * (last - first + 1)


def _frame_kappa(
    a: Sequence[_NamedEvent],
    b: Sequence[_NamedEvent],
    frame_count: int,
    step_ms: float,
) -> float:
    marks_a = bytearray(frame_count)
    marks_b = bytearray(frame_count)
    _mark_frames(marks_a, a, step_ms)
    _mark_frames(marks_b, b, step_ms)
    n11 = n10 = n01 = 0
    for left, right in zip(marks_a, marks_b):
        if left:
            if right:
                n11 += 1
            else:
                n10 += 1
        elif right:
            n01 += 1
    n00 = frame_count - n11 - n10 - n01
    observed = (n11 + n00) / frame_count
    a_positive = (n11 + n10) / frame_count
    b_positive = (n11 + n01) / frame_count
    expected = a_positive * b_positive + (1 - a_positive) * (1 - b_positive)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def _source_identity(project: Project) -> tuple[str, Any]:
    return str(project.source.get("device", "")), project.source.get("createTime")


def compare_projects(
    project_a: Project,
    project_b: Project,
    *,
    iou_threshold: float = 0.5,
    point_tolerance_ms: float = 1000.0,
    frame_step_ms: float = 100.0,
    duration_ms: float | None = None,
    strict_source: bool = True,
    max_frames: int = 20_000_000,
) -> IRRReport:
    """Compute per-label event F1 and interval Cohen's kappa."""

    if not 0 <= iou_threshold <= 1:
        raise IRRConfigurationError("IoU 阈值必须位于 0–1")
    if point_tolerance_ms < 0 or not math.isfinite(point_tolerance_ms):
        raise IRRConfigurationError("点事件容差必须是非负有限数")
    if frame_step_ms < 1 or not math.isfinite(frame_step_ms):
        raise IRRConfigurationError("逐帧步长必须至少为 1ms")
    if strict_source:
        identity_a = _source_identity(project_a)
        identity_b = _source_identity(project_b)
        if identity_a != identity_b and (
            identity_a != ("", None) or identity_b != ("", None)
        ):
            raise IRRSourceMismatchError(
                "两份工程不是同一段数据（device 或 createTime 不一致）"
            )

    events_a = _named_events(project_a)
    events_b = _named_events(project_b)
    all_events = events_a + events_b
    label_order = list(
        dict.fromkeys(event.name for event in all_events)
    )
    type_by_name: dict[str, str] = {}
    for event in all_events:
        previous = type_by_name.setdefault(event.name, event.type)
        if previous != event.type:
            raise IRRConfigurationError(
                f"标签“{event.name}”在两份工程中的类型不一致"
            )

    if duration_ms is None:
        candidates = [
            value for value in (project_a.duration_ms, project_b.duration_ms)
            if value is not None
        ]
        duration_ms = max(candidates, default=0.0)
    if not math.isfinite(duration_ms) or duration_ms < 0:
        raise IRRConfigurationError("IRR 总时长必须是有效非负数")
    for event in all_events:
        _, end = _span(event)
        if event.t1 is None:
            end += point_tolerance_ms
        duration_ms = max(duration_ms, end)
    frame_count = max(1, int(math.ceil(duration_ms / frame_step_ms)))
    if frame_count > max_frames:
        raise IRRConfigurationError(
            f"IRR 逐帧网格需要 {frame_count:,} 帧，超过安全上限 "
            f"{max_frames:,}；请增大 frame_step_ms"
        )

    results: list[IRRLabelResult] = []
    total_tp = total_fp = total_fn = 0
    kappas: list[float] = []
    for name in label_order:
        label_type = type_by_name[name]
        a_items = [event for event in events_a if event.name == name]
        b_items = [event for event in events_b if event.name == name]
        used = [False] * len(b_items)
        matched = 0
        for left in a_items:
            best_index = -1
            best_score = -1.0
            for index, right in enumerate(b_items):
                if used[index]:
                    continue
                if label_type == "point":
                    delta = abs(_centre(left) - _centre(right))
                    if delta > point_tolerance_ms:
                        continue
                    score = 1.0 - delta / (point_tolerance_ms + 1.0)
                else:
                    score = _iou(left, right)
                    if score < iou_threshold:
                        continue
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index >= 0:
                used[best_index] = True
                matched += 1

        false_positive = len(b_items) - matched
        false_negative = len(a_items) - matched
        precision = matched / len(b_items) if b_items else 0.0
        recall = matched / len(a_items) if a_items else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        kappa: float | None = None
        if label_type != "point":
            kappa = _frame_kappa(
                a_items, b_items, frame_count, frame_step_ms
            )
            kappas.append(kappa)
        results.append(
            IRRLabelResult(
                label=name,
                type=label_type,
                n_a=len(a_items),
                n_b=len(b_items),
                matched=matched,
                precision=precision,
                recall=recall,
                event_f1=f1,
                frame_kappa=kappa,
            )
        )
        total_tp += matched
        total_fp += false_positive
        total_fn += false_negative

    micro_precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    micro_recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision + micro_recall
        else 0.0
    )
    return IRRReport(
        rows=tuple(results),
        micro_precision=micro_precision,
        micro_recall=micro_recall,
        micro_f1=micro_f1,
        macro_kappa=sum(kappas) / len(kappas) if kappas else 0.0,
        matched_total=total_tp,
        n_a_total=total_tp + total_fn,
        n_b_total=total_tp + total_fp,
        duration_ms=duration_ms,
        iou_threshold=iou_threshold,
        point_tolerance_ms=point_tolerance_ms,
        frame_step_ms=frame_step_ms,
        annotator_a=project_a.annotator,
        annotator_b=project_b.annotator,
    )


def export_irr_csv(
    report: IRRReport,
    path: str | os.PathLike[str],
) -> Path:
    return _atomic_write_text(Path(path), report.to_csv())


T = TypeVar("T")


class UndoStack(Generic[T]):
    """Bounded snapshot-based undo/redo stack.

    Call ``push(state)`` immediately before a mutation.  ``undo(current)``
    returns the previous snapshot and places ``current`` on the redo stack.
    Snapshots are deep-copied, so later UI mutations cannot alter history.
    """

    def __init__(self, limit: int = 60) -> None:
        if limit < 1:
            raise ValueError("UndoStack limit 必须至少为 1")
        self.limit = int(limit)
        self._undo: list[T] = []
        self._redo: list[T] = []

    @staticmethod
    def _clone(value: T) -> T:
        if isinstance(value, Project):
            return value.clone()  # type: ignore[return-value]
        return copy.deepcopy(value)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_count(self) -> int:
        return len(self._undo)

    @property
    def redo_count(self) -> int:
        return len(self._redo)

    def __len__(self) -> int:
        return len(self._undo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, state: T) -> None:
        self._undo.append(self._clone(state))
        if len(self._undo) > self.limit:
            del self._undo[: len(self._undo) - self.limit]
        self._redo.clear()

    checkpoint = push

    def undo(self, current: T) -> T | None:
        if not self._undo:
            return None
        self._redo.append(self._clone(current))
        return self._clone(self._undo.pop())

    def redo(self, current: T) -> T | None:
        if not self._redo:
            return None
        self._undo.append(self._clone(current))
        return self._clone(self._redo.pop())


# Friendly aliases for UI code and older prototypes.
export_csv = export_events_csv
export_meta = export_meta_json
export_boris = export_boris_csv
compute_irr = compare_projects


def self_test(base_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Exercise v4 round-trip, exports, validation, IRR and undo.

    Returns a machine-readable summary and raises on the first failed
    invariant.  When no directory is supplied all files are temporary.
    """

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if base_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="bovine_annotation_core_")
        root = Path(temporary.name)
    else:
        root = Path(base_dir)
        root.mkdir(parents=True, exist_ok=True)
    try:
        labels = [
            Label(
                "站立", en="standing", type="interval", cat="状态", overlap=False,
                extras={"code": "STANDING", "layer": "body_state"},
            ),
            Label(
                "敲击", en="sync anchor", type="point", cat="锚点",
                extras={"code": "SYNC_ANCHOR", "layer": "sync_anchor", "trainable": False},
            ),
        ]
        project_a = Project(
            annotator="A",
            protocol="v4",
            cow_id="COW-001",
            source={
                "name": "selftest",
                "device": "DEV001",
                "createTime": 1_800_000_000_000,
                "durationMs": 10_000,
                "dataVersion": 4,
                "accScale": 4096,
            },
            align={"method": "anchor", "offsetMs": 125, "videoStartWall": 1},
            videoName="example.mp4",
            labels=copy.deepcopy(labels),
        )
        project_a.add_event(0, 0, 10_000)
        project_a.add_event(1, 5_000)
        new_event = project_a.events[0].to_dict()
        assert "conf" not in new_event and "confidence" not in new_event
        assert "visibility" not in new_event

        project_path = save_project(project_a, root / "project.json")
        loaded = load_project(project_path)
        assert loaded.to_dict() == project_a.to_dict(), "v4 project round-trip failed"
        assert loaded.protocol == "v4" and loaded.cow_id == "COW-001"
        assert loaded.events[0].reviewed_range == {"start": 0.0, "end": 10_000.0}

        legacy = Project.from_dict(
            {
                "_type": PROJECT_TYPE,
                "version": 3,
                "video_name": "legacy.mp4",
                "labels": [{"name": "点", "kind": "point"}],
                "annotations": [
                    {
                        "event_id": 1,
                        "label": "点",
                        "start_ms": 100,
                        "evidence": "curve",
                        "conf": "B",
                        "visibility": "PARTIAL",
                    }
                ],
            }
        )
        assert legacy.version == 4 and legacy.events[0].li == 0
        legacy_event = legacy.events[0].to_dict()
        assert legacy_event["conf"] == "B"
        assert legacy_event["visibility"] == "PARTIAL"

        issues = validate_project(project_a)
        assert not has_validation_errors(issues), issues

        csv_path = export_events_csv(
            project_a,
            root / "events.csv",
            relative_timestamps_ms=[0, 100, 5_000, 10_000],
        )
        meta_path = export_meta_json(project_a, root / "events_meta.json")
        boris_path = export_boris_csv(project_a, root / "events_BORIS.csv")
        event_header = next(
            csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8-sig")))
        )
        assert "t_start_rel_ms" in event_header
        assert "confidence" not in event_header and "visibility" not in event_header
        sample_header = next(
            csv.reader(
                io.StringIO(
                    build_sample_multihot_csv(project_a, [0, 100, 5_000, 10_000])
                )
            )
        )
        assert "tail_negative_eligible" not in sample_header
        assert "DEV001" in meta_path.read_text(encoding="utf-8")
        assert "Observation id" in boris_path.read_text(encoding="utf-8-sig")

        project_b = project_a.clone()
        project_b.annotator = "B"
        project_b.events[0].t1 = 9_800
        project_b.events[1].t0 = 5_200
        report = compare_projects(project_a, project_b)
        assert report.matched_total == 2
        assert report.micro_f1 == 1.0
        irr_path = export_irr_csv(report, root / "IRR_report.csv")
        assert "__overall__" in irr_path.read_text(encoding="utf-8-sig")

        stack: UndoStack[Project] = UndoStack(limit=3)
        stack.push(project_a)
        project_a.events.clear()
        restored = stack.undo(project_a)
        assert restored is not None and len(restored.events) == 2
        redone = stack.redo(restored)
        assert redone is not None and len(redone.events) == 0

        return {
            "ok": True,
            "project_version": PROJECT_VERSION,
            "round_trip_events": len(loaded.events),
            "legacy_migrated": legacy.videoName,
            "validation_issues": len(issues),
            "irr_matched": report.matched_total,
            "irr_micro_f1": report.micro_f1,
            "undo_redo": True,
            "output_dir": str(root),
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="牛尾环标注核心工具")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行无需 Qt 的核心自测",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="自测产物目录；省略时使用并清理临时目录",
    )
    args = parser.parse_args(argv)
    if not args.self_test:
        parser.print_help()
        return 0
    result = self_test(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
