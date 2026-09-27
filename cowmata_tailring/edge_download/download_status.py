"""Per-record local download state and settled day-window markers.

A download round covers the ledger range up to 00:00 (Beijing) of the current
day: today's data is still streaming in from the edge devices, so it is left
to the next day's round. That gives every round a real end, and lets a day
window that finished without failures be recorded as done and skipped by
later rounds instead of being listed from the server again and again.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .core import CHINA, MODALITIES

STATE_DB = "csv-completed.sqlite3"
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")

# Display label, background colour and sort priority (lower is shown first).
STATES = {
    "missing": ("未下载", "#f6d5d1", 0),
    "partial": ("部分已下载", "#f7ebc8", 1),
    "today": ("今日数据 · 明日下载", "#e6e9ef", 2),
    "downloaded": ("已下载", "#d7ecd0", 3),
    "current": ("已下载至昨日 · 佩戴中", "#d7ecd0", 3),
    "invalid": ("不下载 · 传感器无效", "#e4e4e4", 4),
    "excluded": ("不下载", "#e4e4e4", 5),
}


def day_cutoff(now=None):
    """00:00 Beijing time of the current day: the end of every download round."""
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def ensure_table(db):
    db.execute("CREATE TABLE IF NOT EXISTS done_ranges (device TEXT, lo TEXT, hi TEXT, finished TEXT, "
               "ledger TEXT DEFAULT '', local TEXT DEFAULT '', PRIMARY KEY(device, lo, hi))")


def mark_done(db, device, lo, hi, ledger="", local=""):
    ensure_table(db)
    db.execute("INSERT OR REPLACE INTO done_ranges VALUES (?,?,?,?,?,?)",
               (device.upper(), lo.isoformat(), hi.isoformat(), datetime.now(CHINA).isoformat(),
                ledger, local))


def settled(db, device, lo, hi, ledger, local):
    """A past device-day may be skipped only if nothing that decided it changed.

    ``ledger`` fingerprints the sample records that authorised this day and
    ``local`` the verified local originals of this day. A corrected ledger, a
    deleted or modified file each change a fingerprint, so the day is listed
    from the server again and repaired exactly as before.
    """
    ensure_table(db)
    row = db.execute("SELECT ledger, local FROM done_ranges WHERE device=? AND lo=? AND hi=?",
                     (device.upper(), lo.isoformat(), hi.isoformat())).fetchone()
    return bool(row) and row[0] == ledger and row[1] == local


def clear_done(db, device, lo, hi):
    """Forget done markers overlapping [lo, hi) so a forced download lists them again."""
    ensure_table(db)
    rows = db.execute("SELECT lo, hi FROM done_ranges WHERE device=?", (device.upper(),)).fetchall()
    for first, last in rows:
        if datetime.fromisoformat(first) < hi and lo < datetime.fromisoformat(last):
            db.execute("DELETE FROM done_ranges WHERE device=? AND lo=? AND hi=?", (device.upper(), first, last))


def load_done(db):
    ensure_table(db)
    done = {}
    for device, lo, hi in db.execute("SELECT device, lo, hi FROM done_ranges"):
        done.setdefault(device, []).append((datetime.fromisoformat(lo), datetime.fromisoformat(hi)))
    return {device: merge(intervals) for device, intervals in done.items()}


def read_done(root):
    """Done markers of a data root, read without creating anything."""
    path = Path(root) / ".edge-download" / STATE_DB
    if not path.is_file():
        return {}
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='done_ranges'").fetchone()
        return load_done(db) if exists else {}
    except sqlite3.Error:
        return {}
    finally:
        db.close()


def merge(intervals):
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def covered(intervals, lo, hi):
    """True when [lo, hi) lies entirely inside the merged done intervals."""
    if hi <= lo:
        return True
    for start, end in intervals or ():
        if start <= lo < end:
            if hi <= end:
                return True
            lo = end
    return False


def clip_ranges(ranges, selected):
    """Restrict (device, lo, hi) query ranges to the selected record windows."""
    windows = {}
    for record in selected:
        start = datetime.fromisoformat(record["start"])
        end = datetime.fromisoformat(record["end"]) if record.get("end") else None
        windows.setdefault(str(record["device"]).upper(), []).append((start, end))
    pieces = []
    for device, lo, hi in ranges:
        for start, end in windows.get(device.upper(), ()):
            first, last = max(lo, start), min(hi, end or hi)
            if first < last:
                pieces.append((device, first, last))
    merged = []
    for device, lo, hi in sorted(pieces):
        if merged and merged[-1][0] == device and lo <= merged[-1][2]:
            merged[-1] = (device, merged[-1][1], max(merged[-1][2], hi))
        else:
            merged.append((device, lo, hi))
    return merged


def _count_local(root, record, lo, hi):
    """Motion originals of this record inside [lo, hi)."""
    folder, category = record.get("folder") or "", record.get("category") or ""
    if not folder or not category:
        return 0
    base = Path(root) / category / MODALITIES["motion"]
    count, day = 0, lo.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < hi:
        try:
            with os.scandir(base / day.strftime("%Y-%m-%d") / folder) as entries:
                for entry in entries:
                    match = _STAMP.match(entry.name)
                    if match and entry.name.endswith(".json"):
                        stamp = datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=CHINA)
                        count += lo <= stamp < hi
        except OSError:
            pass
        day += timedelta(days=1)
    return count


def local_status(records, root, done=None, now=None):
    """{csv row: dict(state, files)} for every visible sample record."""
    cutoff = day_cutoff(now)
    done = read_done(root) if done is None and root else (done or {})
    result = {}
    for record in records:
        eligibility = record.get("eligibility")
        if eligibility == "excluded":
            state = "invalid" if "无效" in str(record.get("reason", "")) else "excluded"
            result[record["row"]] = dict(state=state, files=0)
            continue
        if eligibility != "eligible":
            continue
        try:
            start = datetime.fromisoformat(record["start"])
            end = datetime.fromisoformat(record["end"]) if record.get("end") else None
        except (KeyError, TypeError, ValueError):
            continue
        last = min(end or cutoff, cutoff)
        files = _count_local(root, record, start, max(last, start)) if root else 0
        if start >= cutoff:
            state = "today"
        elif covered(done.get(str(record["device"]).upper()), start, last):
            state = "downloaded" if end is not None and end <= cutoff else "current"
        elif files:
            state = "partial"
        else:
            state = "missing"
        result[record["row"]] = dict(state=state, files=files)
    return result


def record_day(record):
    try:
        return datetime.fromisoformat(record["start"]).astimezone(CHINA).strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        return ""