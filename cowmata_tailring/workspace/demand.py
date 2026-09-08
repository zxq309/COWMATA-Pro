"""On-demand search hints, never evidence or proof that unsearched video is absent.

Only fully inspected intervals reach the player. Natural file order and sparse
OSD observations prioritize work; neither can rule out an unobserved recording.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath


def natural_key(path):
    return tuple((1, int(p)) if p.isdigit() else (0, p.lower()) for p in re.split(r"(\d+)", path))


def device_name(row):
    if row["metadata"].get("device"):
        return str(row["metadata"]["device"])
    parts = PurePosixPath(row["path"]).parts
    if "九轴" in parts and parts.index("九轴") + 2 < len(parts):
        return parts[parts.index("九轴") + 1]
    return "待读取设备"


def camera_name(row, overrides=None):
    return (overrides or {}).get(row.get("asset_id"), row["metadata"].get("camera")) or PurePosixPath(row["path"]).parts[0]


def source_span(row, hints):
    spans = row["metadata"].get("intervals", [])
    if row["state"] in {"ready", "review"} and spans:
        return min(s["wall_start"] for s in spans), max(s["wall_end"] for s in spans)
    hint = hints.get(row["path"], {})
    start = hint.get("start_ms")
    return (start, hint.get("end_ms")) if start is not None else None


def reference_window(row, start, end, maps, overrides):
    mapping = maps.get(camera_name(row, overrides))
    if mapping is None and not row["metadata"].get("camera"):
        prefix = PurePosixPath(row["path"]).parts[0] + " · "
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
        unknown = [i for i, r in enumerate(group) if (r["path"] not in hints or hints[r["path"]] == {"native_checked": True})
                   and not source_span(r, hints) and r["state"] in {"pending", "invalid"}
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
            hint_choices.append((searched, priority, batch, {**group[pick], "_guided": priority == 0}))
    if full_choices:
        return "full", min(full_choices, key=lambda v: v[0])[1]
    if hint_choices:
        return "hint", min(hint_choices, key=lambda v: v[:3])[3]
    return None


def relevant_rows(rows, start, end, maps=None, overrides=None):
    selected = []
    for row in rows:
        lo, hi = reference_window(row, start, end, maps or {}, overrides or {})
        spans = row["metadata"].get("intervals", [])
        if any(s["wall_start"] <= hi and s["wall_end"] >= lo for s in spans):
            selected.append(row)
    return selected
