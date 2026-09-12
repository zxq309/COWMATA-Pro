"""On-demand search hints, never evidence or proof that unsearched video is absent.

Only fully inspected intervals reach the player. Natural file order and sparse
OSD observations prioritize work; neither can rule out an unobserved recording.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

from .device_identity import source_device_folder


def natural_key(path):
    return tuple((1, int(p)) if p.isdigit() else (0, p.lower()) for p in re.split(r"(\d+)", path))


def device_aliases(rows):
    """UI grouping for unread legacy short-code folders, never cow identity."""
    groups = defaultdict(set)
    for row in rows:
        if row["kind"] != "imu" or row["state"] in {"ignored", "missing"}:
            continue
        folder = source_device_folder(row["path"])
        device = str(row["metadata"].get("device", ""))
        if folder and re.fullmatch(r"[0-9a-fA-F]{12}", device):
            groups[folder.as_posix()].add(device.upper())
    return {folder: next(iter(devices)) for folder, devices in groups.items() if len(devices) == 1}


def device_name(row, aliases=None):
    if row["metadata"].get("device"):
        name = str(row["metadata"]["device"])
        return name.upper() if re.fullmatch(r"[0-9a-fA-F]{12}", name) else name
    folder = source_device_folder(row["path"])
    if folder:
        name = folder.name
        # Device exports append model/batch names to the hardware ID. Keep
        # unread records in the same group after the first JSON is decoded.
        match = re.fullmatch(r"([0-9a-fA-F]{12})(?:[-_].+)?", name)
        if match:
            return match[1].upper()
        prefix = name.split("-", 1)[0]
        known = (aliases or {}).get(folder.as_posix())
        if known and re.fullmatch(r"[0-9a-fA-F]{4,11}", prefix) and known.endswith(prefix.upper()):
            return known
        return name
    return "待读取设备"


def camera_name(row, overrides=None):
    override = (overrides or {}).get(row.get("asset_id"))
    if override:
        return stable_camera_name(override, camera_folder(row['path']))
    metadata = row["metadata"]
    name = metadata.get("camera")
    folder = camera_folder(row["path"])
    # Structured resource folders are stable camera identities. Resolution and
    # OSD placement may change within the same camera/card-copy batch.
    if re.match(r"^视角\d", folder) and not metadata.get("manual_readings"):
        if not name or re.fullmatch(r".+ · (?:\d+|None)×(?:\d+|None) · [a-z_]+", name):
            return folder
    return stable_camera_name(name or folder, folder)


def stable_camera_name(name, folder):
    """Resolution and OCR location describe a recording, not a camera identity."""
    match = re.fullmatch(r'(.+) · (?:\d+|None)×(?:\d+|None) · [a-z_]+',name)
    return folder if match and match[1] == folder else name


def camera_folder(relative):
    parts = PurePosixPath(relative).parts
    return next((p for p in parts[:-1] if re.match(r"^视角\d", p)),
                parts[0] if len(parts) > 1 else "录像")


def camera_inventory(rows, overrides=None):
    """Keep source views visible independently of the active record's coverage.

    Unread rows may use the one known camera in their folder as a UI identity;
    this does not create timestamps, intervals or camera calibration.
    """
    videos = [r for r in rows if r["kind"] == "video" and r["state"] not in {"missing", "ignored"}]
    known = defaultdict(set)
    for row in videos:
        if row["metadata"].get("camera") or (overrides or {}).get(row.get("asset_id")):
            known[camera_folder(row["path"])].add(camera_name(row, overrides))
    result = defaultdict(list)
    for row in videos:
        name = camera_name(row, overrides)
        candidates = known[camera_folder(row["path"])]
        if not row["metadata"].get("camera") and not (overrides or {}).get(row.get("asset_id")) and len(candidates) == 1:
            name = next(iter(candidates))
        result[name].append(row)
    return dict(result)


def resolve_camera_choices(names, inventory):
    """Retain checked folder placeholders after an unambiguous first inspection."""
    folders = defaultdict(set)
    for camera, rows in inventory.items():
        for row in rows:
            folders[camera_folder(row['path'])].add(camera)
    result = []
    for name in names:
        for folder in folders:
            name = stable_camera_name(name,folder)
        candidates = folders.get(name, set())
        resolved = name if name in inventory else next(iter(candidates)) if len(candidates) == 1 else None
        if resolved is not None and resolved not in result:
            result.append(resolved)
    return result


def source_span(row, hints):
    spans = row["metadata"].get("intervals", [])
    if spans and (row["state"] in {"ready", "review"} or
                  row["state"] == "pending" and row.get("asset_id") and row["metadata"].get("recheck")):
        # Unchanged legacy sources retain useful routing times while their
        # derived index is rechecked. intervals_from_rows still excludes them
        # from playback/evidence until inspection succeeds.
        return min(s["wall_start"] for s in spans), max(s["wall_end"] for s in spans)
    hint = hints.get(row["path"], {})
    start = hint.get("start_ms")
    return (start, hint.get("end_ms")) if start is not None else None


def reference_window(row, start, end, maps, overrides):
    mapping = maps.get(camera_name(row, overrides))
    if mapping is None:
        prefix = camera_folder(row["path"]) + " · "
        matches = [value for name, value in maps.items() if name.startswith(prefix)]
        if len(matches) == 1:
            mapping = matches[0]
    if mapping and mapping.anchors:
        start, end = mapping.map(start, inverse=True), mapping.map(end, inverse=True)
    return min(start, end), max(start, end)


def next_video_task(rows, hints, start, end, *, maps=None, overrides=None, attempted=(), explore=True):
    """Return (full|hint, row). One source at a time; caller limits discovery.

    Parent folders isolate card-copy batches whose counters can reset. For a
    sequential batch, bisect toward the requested time and inspect its previous
    clip as well. Disorder/unknown endpoints fall back to unsampled positions.
    """
    maps, overrides = maps or {}, overrides or {}
    attempted = set(attempted)
    groups = defaultdict(list)
    for row in rows:
        if row["kind"] == "video" and row["state"] not in {"missing", "ignored"}:
            groups[str(PurePosixPath(row["path"]).parent)].append(row)
    hint_choices = []
    full_choices = []
    for batch, group in sorted(groups.items()):
        group.sort(key=lambda r: natural_key(r["path"]))
        lo, hi = reference_window(group[0], start, end, maps, overrides)
        known = [(i, source_span(r, hints)) for i, r in enumerate(group)]
        known = [(i, span) for i, span in known if span is not None]
        starts = [(i, span[0]) for i, span in known]
        # Both edges and the predecessor matter. Estimated header end times
        # are NEVER sufficient to exclude a candidate before full inspection.
        before = [(i, t) for i, t in starts if t <= lo]
        predecessor = max(before, key=lambda p: p[1])[0] if before else None
        for i, span in known:
            row = group[i]
            overlaps = lo <= span[0] <= hi or span[1] is not None and span[0] <= hi and span[1] >= lo
            bracketed = span[1] is None and i == predecessor and (i == len(group) - 1 or any(j == i + 1 and t > lo for j, t in starts))
            if (overlaps or bracketed) and row["state"] in {"pending", "invalid"} and row["path"] not in attempted:
                full_choices.append((abs(span[0] - lo), row))
        unknown = [i for i, r in enumerate(group) if not source_span(r, hints) and r["state"] in {"pending", "invalid"}
                   and r["path"] not in attempted]
        if not unknown:
            continue
        # First/last provide anchors, not a promise of monotonicity.
        pick, priority = unknown[0], 4
        if not starts:
            pick, priority = unknown[0], 1
        elif len(group) - 1 in unknown:
            pick, priority = len(group) - 1, 2
        else:
            ordered = all(a[1] <= b[1] for a, b in zip(starts, starts[1:]))
            if ordered:
                left = max((i for i, t in starts if t < lo), default=-1)
                right = min((i for i, t in starts if t > hi), default=len(group))
                inside = [i for i in unknown if left < i < right]
                if inside:
                    # Once a hit exists, fill adjacent clips to preserve joins.
                    hits = [i for i, t in starts if lo <= t <= hi]
                    pick = min(inside, key=lambda i: min(abs(i - j) for j in hits)) if hits else inside[len(inside) // 2]
                    priority = 0
                elif predecessor is not None and predecessor - 1 in unknown:
                    pick, priority = predecessor - 1, 0
        # Round-robin batches; one long/irrelevant camera cannot use the budget.
        searched = sum(r["path"] in hints or source_span(r, hints) is not None for r in group)
        if explore or priority == 0:
            row = group[pick]
            hint = hints.get(row["path"])
            # A failed opening read is not a failed video. Full inspection
            # samples later frames; charge this fallback to exploration too.
            fallback = hint is not None and hint != {"native_checked": True}
            hint_choices.append((searched, priority, batch, "full" if fallback else "hint",
                                 {**row, "_guided": priority == 0, "_exploratory": fallback and priority != 0}))
    if full_choices:
        return "full", min(full_choices, key=lambda v: v[0])[1]
    if hint_choices:
        choice = min(hint_choices, key=lambda v: v[:3])
        return choice[3], choice[4]
    return None


def relevant_rows(rows, start, end, maps=None, overrides=None):
    selected = []
    for row in rows:
        lo, hi = reference_window(row, start, end, maps or {}, overrides or {})
        spans = row["metadata"].get("intervals", [])
        if any(s["wall_start"] <= hi and s["wall_end"] >= lo for s in spans):
            selected.append(row)
    return selected
