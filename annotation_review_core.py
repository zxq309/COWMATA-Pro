"""Pure data helpers for reviewing exported event annotations.

This module deliberately has no Qt dependency.  It parses the tool's
``*.events.csv`` exports, resolves their source IMU JSON files, persists a
batch review workspace, and writes corrected copies without overwriting the
original exports.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from defaults import DEFAULT_LABELS, DEFAULT_PROTOCOL


REVIEW_WORKSPACE_SCHEMA = 1
REVIEW_SOURCE = "events_csv"
REVIEW_STATUSES = {"pending", "modified", "reviewed"}
JSON_MATCH_STATUSES = {
    "unresolved",
    "resolved",
    "missing",
    "ambiguous",
    "fingerprint_mismatch",
}
EVIDENCE_TO_INTERNAL = {
    "两者": "both",
    "仅视频": "video",
    "仅曲线": "curve",
    "both": "both",
    "video": "video",
    "curve": "curve",
}
EVIDENCE_TO_EXPORT = {
    "both": "两者",
    "video": "仅视频",
    "curve": "仅曲线",
}
EVENT_CSV_COLUMNS = [
    "index",
    "session_id",
    "cow_id",
    "layer",
    "label",
    "code",
    "en",
    "type",
    "evidence",
    "annotator",
    "protocol",
    "t_start_rel_ms",
    "t_end_rel_ms",
    "reviewed_start_ms",
    "reviewed_end_ms",
    "duration_ms",
    "frame_start",
    "frame_end",
    "t_start_wall_bj",
    "t_end_wall_bj",
    "note",
]
REVIEW_REPORT_COLUMNS = [
    "review_uid",
    "status",
    "modified",
    "deleted",
    "session_id",
    "device",
    "cow_id",
    "source_csv",
    "source_json",
    "original_code",
    "current_code",
    "original_start_ms",
    "current_start_ms",
    "original_end_ms",
    "current_end_ms",
    "reviewed_by",
    "reviewed_at",
    "note",
]
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ReviewImportError(ValueError):
    """Raised when an event CSV cannot be safely reconstructed."""


def _finite_number(
    value: Any,
    field: str,
    *,
    allow_blank: bool = False,
) -> float | None:
    if allow_blank and value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReviewImportError(f"{field} 不是有效数字：{value!r}") from exc
    if not math.isfinite(number):
        raise ReviewImportError(f"{field} 必须是有限数字")
    return number


def _session_key(csv_path: Path) -> str:
    text = os.path.normcase(str(csv_path.resolve()))
    return hashlib.sha1(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]


def _meta_path_for_csv(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.name[:-4] + "_meta.json")


def _read_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_beijing_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=BEIJING)
        except ValueError:
            continue
        return int(round(parsed.timestamp() * 1000.0))
    return 0


def _legacy_label(row: Mapping[str, Any]) -> dict[str, Any]:
    code = str(row.get("code", "") or row.get("label", "")).strip().upper()
    name = str(row.get("label", "") or code or "未知旧标签").strip()
    layer = str(row.get("layer", "") or "legacy_annotation").strip()
    label_type = str(row.get("type", "") or "interval").strip().lower()
    if label_type not in {"point", "interval"}:
        label_type = "interval"
    return {
        "name": name,
        "en": str(row.get("en", "") or ""),
        "code": code or name,
        "key": "",
        "color": "#b7791f",
        "type": label_type,
        "cat": "历史标签",
        "layer": layer,
        "minDur": 0.0,
        "overlap": True,
        "specialty": True,
        "trainable": False,
        "legacy": True,
        "def": "从旧版事件 CSV 恢复的历史标签；请在复核时确认是否改为当前标签。",
    }


def _label_index_for_row(
    labels: list[dict[str, Any]], row: Mapping[str, Any]
) -> tuple[int, bool]:
    code = str(row.get("code", "") or "").strip().upper()
    name = str(row.get("label", "") or "").strip()
    for index, label in enumerate(labels):
        label_code = str(label.get("code", "") or "").strip().upper()
        if code and label_code == code:
            return index, bool(label.get("legacy", False))
    for index, label in enumerate(labels):
        if name and str(label.get("name", "")).strip() == name:
            return index, bool(label.get("legacy", False))
    legacy = _legacy_label(row)
    labels.append(legacy)
    return len(labels) - 1, True


def _review_uid(session_key: str, row_index: int) -> str:
    return f"{session_key}:{row_index}"


def load_events_csv(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load one exported event CSV into a serializable review session."""

    csv_path = Path(path).expanduser().resolve()
    if not csv_path.is_file():
        raise ReviewImportError(f"CSV 文件不存在：{csv_path}")
    if not csv_path.name.casefold().endswith(".events.csv"):
        raise ReviewImportError("仅支持本工具导出的 *.events.csv")
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns = [str(value) for value in (reader.fieldnames or [])]
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReviewImportError(f"CSV 读取失败：{exc}") from exc
    required = {"session_id", "label", "t_start_rel_ms", "t_end_rel_ms"}
    missing = sorted(required.difference(columns))
    if missing:
        raise ReviewImportError("CSV 缺少字段：" + "、".join(missing))
    if not rows:
        raise ReviewImportError("CSV 中没有标注事件")

    session_ids = {
        str(row.get("session_id", "") or "").strip() for row in rows
    }
    session_ids.discard("")
    if len(session_ids) > 1:
        raise ReviewImportError("单个 CSV 包含多个 session_id，无法安全关联 JSON")
    default_session = csv_path.name[: -len(".events.csv")]
    session_id = next(iter(session_ids), default_session)
    key = _session_key(csv_path)
    meta_path = _meta_path_for_csv(csv_path)
    meta = _read_meta(meta_path)
    labels = copy.deepcopy(DEFAULT_LABELS)
    events: list[dict[str, Any]] = []

    for ordinal, row in enumerate(rows, start=1):
        raw_start = _finite_number(
            row.get("t_start_rel_ms"),
            f"第 {ordinal} 行开始时间",
        )
        raw_end = _finite_number(
            row.get("t_end_rel_ms"),
            f"第 {ordinal} 行结束时间",
            allow_blank=True,
        )
        reviewed_start = _finite_number(
            row.get("reviewed_start_ms"),
            f"第 {ordinal} 行复核开始时间",
            allow_blank=True,
        )
        reviewed_end = _finite_number(
            row.get("reviewed_end_ms"),
            f"第 {ordinal} 行复核结束时间",
            allow_blank=True,
        )
        # The training/runtime readers use the reviewed range when it is
        # present.  Import the same effective interval so a prior correction
        # is not accidentally shown at the stale prediction boundary.
        start = raw_start if reviewed_start is None else reviewed_start
        end = raw_end if reviewed_end is None else reviewed_end
        assert start is not None
        if start < 0 or (end is not None and end < start):
            raise ReviewImportError(f"第 {ordinal} 行起止时间范围无效")
        label_index, legacy = _label_index_for_row(labels, row)
        label = labels[label_index]
        code = str(label.get("code", row.get("code", "")))
        event = {
            "id": ordinal,
            "li": label_index,
            "label_code": code,
            "layer": str(label.get("layer", row.get("layer", ""))),
            "t0": float(start),
            "t1": None if end is None else float(end),
            "note": str(row.get("note", "") or ""),
            "ev": EVIDENCE_TO_INTERNAL.get(
                str(row.get("evidence", "") or "").strip(), "both"
            ),
            "ctx": "",
            "reviewed_range": {
                "start": float(start),
                "end": None if end is None else float(end),
            },
            "review_source": REVIEW_SOURCE,
            "review_uid": _review_uid(key, ordinal),
            "review_session_key": key,
            "review_status": "pending",
            "review_modified": False,
            "review_deleted": False,
            "review_legacy_label": legacy,
            "review_original": {
                "code": str(row.get("code", "") or code),
                "label": str(row.get("label", "") or label.get("name", "")),
                "start_ms": float(start),
                "end_ms": None if end is None else float(end),
                "note": str(row.get("note", "") or ""),
                "exported_start_ms": (
                    None if raw_start is None else float(raw_start)
                ),
                "exported_end_ms": (
                    None if raw_end is None else float(raw_end)
                ),
            },
            "review_original_row": dict(row),
        }
        frame_start = str(row.get("frame_start", "") or "").strip()
        frame_end = str(row.get("frame_end", "") or "").strip()
        if frame_start:
            try:
                event["json_sample_start"] = int(frame_start)
            except ValueError:
                pass
        if frame_end:
            try:
                event["json_sample_end"] = int(frame_end)
            except ValueError:
                pass
        events.append(event)

    first = rows[0]
    try:
        acc_scale = int(float(meta.get("acc_scale_divisor", 4096) or 4096))
    except (TypeError, ValueError):
        acc_scale = 4096
    return {
        "key": key,
        "session_id": session_id,
        "csv_path": str(csv_path),
        "csv_columns": columns or list(EVENT_CSV_COLUMNS),
        "meta_path": str(meta_path) if meta_path.is_file() else "",
        "json_path": "",
        "json_candidates": [],
        "json_match_status": "unresolved",
        "device": str(meta.get("device", "") or ""),
        "cow_id": str(first.get("cow_id", "") or meta.get("cow_id", "")),
        "annotator": str(
            first.get("annotator", "") or meta.get("annotator", "")
        ),
        "protocol": str(
            first.get("protocol", "") or meta.get("protocol", DEFAULT_PROTOCOL)
        ),
        "video_name": str(meta.get("video_name", "") or ""),
        "create_time_ms": _parse_beijing_ms(meta.get("create_time_bj")),
        "acc_scale": acc_scale,
        "source_meta": meta,
        "labels": labels,
        "events": events,
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def new_workspace(json_root: str = "") -> dict[str, Any]:
    return {
        "_type": "bovine-annotation-review-workspace",
        "schema": REVIEW_WORKSPACE_SCHEMA,
        "json_root": str(json_root or ""),
        "sessions": [],
        "last_event_uid": "",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def import_csv_files(
    workspace: dict[str, Any], paths: Iterable[str | os.PathLike[str]]
) -> tuple[int, int, list[str]]:
    sessions = workspace.setdefault("sessions", [])
    existing = {
        os.path.normcase(str(Path(item.get("csv_path", "")).resolve()))
        for item in sessions
        if isinstance(item, dict) and item.get("csv_path")
    }
    imported = 0
    skipped = 0
    errors: list[str] = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        identity = os.path.normcase(str(path))
        if identity in existing:
            skipped += 1
            continue
        try:
            session = load_events_csv(path)
        except ReviewImportError as exc:
            errors.append(f"{path.name}：{exc}")
            continue
        sessions.append(session)
        existing.add(identity)
        imported += 1
    workspace["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    return imported, skipped, errors


def infer_json_root(paths: Iterable[str | os.PathLike[str]]) -> Path | None:
    for value in paths:
        csv_path = Path(value).expanduser().resolve()
        start = csv_path.parent
        stem = csv_path.name
        if stem.casefold().endswith(".events.csv"):
            stem = stem[: -len(".events.csv")]
        # Small exports are often kept beside their source JSON.  Resolve
        # this common layout without forcing the user to choose a directory.
        direct_candidates = [start / f"{stem}.json"]
        if stem.casefold().endswith(".reviewed"):
            direct_candidates.append(start / f"{stem[:-len('.reviewed')]}.json")
        if any(candidate.is_file() for candidate in direct_candidates):
            return start
        for ancestor in (start, *list(start.parents)[:4]):
            candidate = ancestor / "九轴数据"
            if candidate.is_dir():
                return candidate
    return None


def build_json_index(root: str | os.PathLike[str]) -> dict[str, list[Path]]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return {}
    result: dict[str, list[Path]] = {}
    try:
        candidates = base.rglob("*.json")
        for path in candidates:
            name = path.name.casefold()
            if name.endswith(".annotation.json") or name.endswith(
                ".events_meta.json"
            ):
                continue
            result.setdefault(name, []).append(path.resolve())
    except OSError:
        return result
    return result


def source_file_fingerprint(
    path: str | os.PathLike[str], *, chunk_size: int = 65_536
) -> str:
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with source.open("rb") as handle:
        digest.update(handle.read(chunk_size))
        if stat.st_size > chunk_size:
            handle.seek(max(0, stat.st_size - chunk_size))
            digest.update(handle.read(chunk_size))
    return "sha256-partial:" + digest.hexdigest()


def _candidate_score(session: Mapping[str, Any], candidate: Path) -> int:
    text = str(candidate).casefold()
    score = 0
    device = str(session.get("device", "") or "").strip().casefold()
    cow_id = str(session.get("cow_id", "") or "").strip().casefold()
    session_id = str(session.get("session_id", "") or "")
    if device and device in text:
        score += 4
    if cow_id and cow_id in text:
        score += 2
    if len(session_id) >= 10 and session_id[:10] in text:
        score += 1
    return score


def resolve_session_json(
    session: dict[str, Any], json_index: Mapping[str, list[Path]]
) -> str:
    meta = session.get("source_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    saved_path = str(meta.get("source_json_path", "") or "").strip()
    expected_fingerprint = str(
        meta.get("source_json_fingerprint", "") or ""
    ).strip()
    if saved_path:
        candidate = Path(saved_path).expanduser()
        if not candidate.is_absolute() and session.get("meta_path"):
            candidate = Path(session["meta_path"]).parent / candidate
        if candidate.is_file():
            resolved = candidate.resolve()
            if expected_fingerprint:
                try:
                    fingerprint_matches = (
                        source_file_fingerprint(resolved) == expected_fingerprint
                    )
                except OSError:
                    fingerprint_matches = False
                if not fingerprint_matches:
                    session["json_path"] = ""
                    session["json_candidates"] = [str(resolved)]
                    session["json_match_status"] = "fingerprint_mismatch"
                    return "fingerprint_mismatch"
            session["json_path"] = str(resolved)
            session["json_candidates"] = [str(resolved)]
            session["json_match_status"] = "resolved"
            return "resolved"

    file_name = str(session.get("session_id", "") or "").strip() + ".json"
    candidates = list(json_index.get(file_name.casefold(), []))
    if expected_fingerprint and len(candidates) > 1:
        matching: list[Path] = []
        for candidate in candidates:
            try:
                if source_file_fingerprint(candidate) == expected_fingerprint:
                    matching.append(candidate)
            except OSError:
                continue
        if matching:
            candidates = matching
        else:
            session["json_path"] = ""
            session["json_candidates"] = [str(path) for path in candidates]
            session["json_match_status"] = "fingerprint_mismatch"
            return "fingerprint_mismatch"
    elif expected_fingerprint and len(candidates) == 1:
        try:
            matches = source_file_fingerprint(candidates[0]) == expected_fingerprint
        except OSError:
            matches = False
        if not matches:
            session["json_path"] = ""
            session["json_candidates"] = [str(path) for path in candidates]
            session["json_match_status"] = "fingerprint_mismatch"
            return "fingerprint_mismatch"

    session["json_candidates"] = [str(path) for path in candidates]
    if not candidates:
        session["json_path"] = ""
        session["json_match_status"] = "missing"
        return "missing"
    if len(candidates) == 1:
        session["json_path"] = str(candidates[0])
        session["json_match_status"] = "resolved"
        return "resolved"

    scores = [(_candidate_score(session, path), path) for path in candidates]
    best_score = max(score for score, _path in scores)
    best = [path for score, path in scores if score == best_score]
    if best_score > 0 and len(best) == 1:
        session["json_path"] = str(best[0])
        session["json_match_status"] = "resolved"
        return "resolved"
    session["json_path"] = ""
    session["json_match_status"] = "ambiguous"
    return "ambiguous"


def resolve_workspace_jsons(
    workspace: dict[str, Any], root: str | os.PathLike[str]
) -> dict[str, int]:
    base = Path(root).expanduser().resolve()
    workspace["json_root"] = str(base)
    index = build_json_index(base)
    counts = {"resolved": 0, "missing": 0, "ambiguous": 0}
    for session in workspace.get("sessions", []):
        if not isinstance(session, dict):
            continue
        status = resolve_session_json(session, index)
        counts[status] = counts.get(status, 0) + 1
    workspace["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    return counts


def review_display_status(
    session: Mapping[str, Any], event: Mapping[str, Any]
) -> str:
    match_status = str(session.get("json_match_status", "unresolved"))
    if match_status == "missing":
        return "缺少 JSON"
    if match_status == "ambiguous":
        return "JSON 待选择"
    if match_status == "fingerprint_mismatch":
        return "JSON 指纹不一致"
    if bool(event.get("review_deleted", False)):
        return "已删除"
    status = str(event.get("review_status", "pending"))
    modified = bool(event.get("review_modified", False))
    if status == "reviewed":
        return "已通过（修改）" if modified else "已通过"
    if modified or status == "modified":
        return "已修改"
    return "待复核"


def iter_review_events(
    workspace: Mapping[str, Any],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for session in workspace.get("sessions", []):
        if not isinstance(session, dict):
            continue
        events = session.get("events", [])
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict):
                yield session, event


def review_counts(workspace: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "total": 0,
        "pending": 0,
        "modified": 0,
        "reviewed": 0,
        "missing_json": 0,
        "deleted": 0,
    }
    for session, event in iter_review_events(workspace):
        counts["total"] += 1
        display = review_display_status(session, event)
        if display in {"缺少 JSON", "JSON 待选择", "JSON 指纹不一致"}:
            counts["missing_json"] += 1
        elif display == "已删除":
            counts["deleted"] += 1
            counts["modified"] += 1
        elif display.startswith("已通过"):
            counts["reviewed"] += 1
        elif display == "已修改":
            counts["modified"] += 1
        else:
            counts["pending"] += 1
    return counts


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return path


def save_workspace(
    workspace: dict[str, Any], path: str | os.PathLike[str]
) -> Path:
    workspace["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    return _atomic_json(Path(path).expanduser().resolve(), workspace)


def load_workspace(path: str | os.PathLike[str]) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise ReviewImportError(f"复核工作区读取失败：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("_type") != (
        "bovine-annotation-review-workspace"
    ):
        raise ReviewImportError("不是本工具的标注复核工作区")
    try:
        schema = int(payload.get("schema", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ReviewImportError("复核工作区 schema 字段无效") from exc
    if schema != REVIEW_WORKSPACE_SCHEMA:
        raise ReviewImportError("不支持的标注复核工作区版本")
    if not isinstance(payload.get("sessions"), list):
        raise ReviewImportError("复核工作区 sessions 字段无效")
    for index, session in enumerate(payload["sessions"], start=1):
        if not isinstance(session, dict):
            raise ReviewImportError(f"第 {index} 个复核会话不是对象")
        events = session.get("events", [])
        if not isinstance(events, list):
            raise ReviewImportError(f"第 {index} 个复核会话 events 字段无效")
        if not all(isinstance(event, dict) for event in events):
            raise ReviewImportError(f"第 {index} 个复核会话包含无效事件")
        labels = session.get("labels", [])
        if not isinstance(labels, list) or not all(
            isinstance(label, dict) for label in labels
        ):
            raise ReviewImportError(f"第 {index} 个复核会话 labels 字段无效")
        status = str(session.get("json_match_status", "unresolved"))
        if status not in JSON_MATCH_STATUSES:
            session["json_match_status"] = "unresolved"
    return payload


def _unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"无法为输出文件生成不重复名称：{path}")


def _safe_file_stem(value: Any, fallback: str = "session") -> str:
    """Make a session id safe to use as a filename on every platform."""

    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.rstrip(" .")
    if text in {"", ".", ".."}:
        text = fallback
    return text[:180]


def _format_wall_ms(value_ms: float | int | None) -> str:
    if value_ms in (None, ""):
        return ""
    try:
        value = float(value_ms)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    moment = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(
        BEIJING
    )
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def _label_for_event(
    session: Mapping[str, Any], event: Mapping[str, Any]
) -> Mapping[str, Any]:
    labels = session.get("labels", [])
    index = int(event.get("li", -1) or -1)
    if isinstance(labels, list) and 0 <= index < len(labels):
        label = labels[index]
        if isinstance(label, dict):
            return label
    return {}


def _corrected_row(
    session: Mapping[str, Any], event: Mapping[str, Any], ordinal: int
) -> dict[str, Any]:
    original = event.get("review_original_row", {})
    row = dict(original) if isinstance(original, dict) else {}
    label = _label_for_event(session, event)
    start = float(event.get("t0", 0.0))
    end_value = event.get("t1")
    end = None if end_value is None else float(end_value)
    create_time = int(session.get("create_time_ms", 0) or 0)
    row.update(
        {
            "index": ordinal,
            "session_id": str(session.get("session_id", "")),
            "cow_id": str(session.get("cow_id", "")),
            "layer": str(label.get("layer", event.get("layer", ""))),
            "label": str(label.get("name", event.get("label_code", ""))),
            "code": str(label.get("code", event.get("label_code", ""))),
            "en": str(label.get("en", "")),
            "type": str(label.get("type", "point" if end is None else "interval")),
            "evidence": EVIDENCE_TO_EXPORT.get(str(event.get("ev", "both")), "两者"),
            "annotator": str(session.get("annotator", "")),
            "protocol": str(session.get("protocol", DEFAULT_PROTOCOL)),
            "t_start_rel_ms": round(start),
            "t_end_rel_ms": "" if end is None else round(end),
            "reviewed_start_ms": round(start),
            "reviewed_end_ms": "" if end is None else round(end),
            "duration_ms": "" if end is None else round(end - start),
            "frame_start": event.get("json_sample_start", row.get("frame_start", "")),
            "frame_end": (
                ""
                if end is None
                else event.get("json_sample_end", row.get("frame_end", ""))
            ),
            "t_start_wall_bj": (
                _format_wall_ms(create_time + start)
                if create_time
                else row.get("t_start_wall_bj", "")
            ),
            "t_end_wall_bj": (
                ""
                if end is None
                else (
                    _format_wall_ms(create_time + end)
                    if create_time
                    else row.get("t_end_wall_bj", "")
                )
            ),
            "note": str(event.get("note", "")),
        }
    )
    return row


def _write_csv(path: Path, columns: list[str], rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return path


def export_review_results(
    workspace: Mapping[str, Any], output_directory: str | os.PathLike[str]
) -> list[Path]:
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    report_rows: list[dict[str, Any]] = []

    for session in workspace.get("sessions", []):
        if not isinstance(session, dict):
            continue
        active_events = [
            event
            for event in session.get("events", [])
            if isinstance(event, dict) and not event.get("review_deleted", False)
        ]
        active_events.sort(
            key=lambda event: (float(event.get("t0", 0.0)), int(event.get("id", 0)))
        )
        base_name = _safe_file_stem(session.get("session_id", "session"))
        target = _unique_output_path(output / f"{base_name}.reviewed.events.csv")
        columns = [str(value) for value in session.get("csv_columns", [])]
        if not columns:
            columns = list(EVENT_CSV_COLUMNS)
        rows = [
            _corrected_row(session, event, ordinal)
            for ordinal, event in enumerate(active_events, start=1)
        ]
        created.append(_write_csv(target, columns, rows))

        meta = copy.deepcopy(session.get("source_meta", {}))
        if not isinstance(meta, dict):
            meta = {}
        json_path = str(session.get("json_path", "") or "")
        meta.update(
            {
                "source_json_path": json_path,
                "source_json_name": Path(json_path).name if json_path else "",
                "review_workspace_schema": REVIEW_WORKSPACE_SCHEMA,
                "reviewed_export_bj": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                "review_counts": {
                    "total": len(session.get("events", [])),
                    "exported": len(active_events),
                    "reviewed": sum(
                        1
                        for event in session.get("events", [])
                        if isinstance(event, dict)
                        and event.get("review_status") == "reviewed"
                    ),
                },
            }
        )
        if json_path and Path(json_path).is_file():
            try:
                stat = Path(json_path).stat()
                meta["source_json_size"] = stat.st_size
                meta["source_json_mtime_ns"] = stat.st_mtime_ns
                meta["source_json_fingerprint"] = source_file_fingerprint(json_path)
            except OSError:
                pass
        meta_target = Path(str(target.with_suffix("")) + "_meta.json")
        created.append(_atomic_json(meta_target, meta))

        for event in session.get("events", []):
            if not isinstance(event, dict):
                continue
            original = event.get("review_original", {})
            if not isinstance(original, dict):
                original = {}
            label = _label_for_event(session, event)
            report_rows.append(
                {
                    "review_uid": event.get("review_uid", ""),
                    "status": review_display_status(session, event),
                    "modified": int(bool(event.get("review_modified", False))),
                    "deleted": int(bool(event.get("review_deleted", False))),
                    "session_id": session.get("session_id", ""),
                    "device": session.get("device", ""),
                    "cow_id": session.get("cow_id", ""),
                    "source_csv": session.get("csv_path", ""),
                    "source_json": session.get("json_path", ""),
                    "original_code": original.get("code", ""),
                    "current_code": label.get("code", event.get("label_code", "")),
                    "original_start_ms": original.get("start_ms", ""),
                    "current_start_ms": event.get("t0", ""),
                    "original_end_ms": original.get("end_ms", ""),
                    "current_end_ms": event.get("t1", ""),
                    "reviewed_by": event.get("reviewed_by", ""),
                    "reviewed_at": event.get("reviewed_at", ""),
                    "note": event.get("note", ""),
                }
            )

    report_target = _unique_output_path(output / "annotation_review_report.csv")
    created.append(_write_csv(report_target, REVIEW_REPORT_COLUMNS, report_rows))
    return created


__all__ = [
    "EVENT_CSV_COLUMNS",
    "JSON_MATCH_STATUSES",
    "REVIEW_SOURCE",
    "REVIEW_STATUSES",
    "REVIEW_WORKSPACE_SCHEMA",
    "ReviewImportError",
    "build_json_index",
    "export_review_results",
    "import_csv_files",
    "infer_json_root",
    "iter_review_events",
    "load_events_csv",
    "load_workspace",
    "new_workspace",
    "resolve_session_json",
    "resolve_workspace_jsons",
    "review_counts",
    "review_display_status",
    "save_workspace",
    "source_file_fingerprint",
]
