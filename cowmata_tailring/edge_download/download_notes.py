"""Persistent download-side notes kept separate from the three ledger CSVs.

Every note stays queryable across restarts and ledger refreshes: the archive
upserts by (kind, source, record ID, device), records what changed between
snapshots, and marks entries no longer present in the current plan as
history instead of deleting them.  The original CSV and Excel files are
never touched.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .core import CHINA
from .settings import atomic_json

SCHEMA = "cowmata-download-notes-v1"
KEY_FIELDS = ("kind", "source", "record_id", "device")
TRACE_FIELDS = ("row", "cow", "cow_text", "field", "text", "message",
                "download_start", "download_end")


def notes_path(data_root):
    return Path(data_root) / ".edge-download" / "download-notes.json"


def note_key(note):
    record_id = str(note.get("record_id", "") or "")
    return (str(note.get("kind", "")), str(note.get("source", "")),
            record_id or "row:" + str(note.get("row", "")),
            str(note.get("device", "")))


class NotesStore:
    def __init__(self, path):
        self.path = Path(path)
        self.records = self._read()

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            return []
        notes = value.get("notes")
        if not isinstance(notes, list):
            return []
        return [dict(record) for record in notes if isinstance(record, dict)]

    def merge(self, notes, fingerprint=""):
        """Upsert the current plan's notes; absent entries become history."""
        now = datetime.now(CHINA).isoformat(timespec="seconds")
        index = {note_key(record): record for record in self.records}
        for note in notes:
            key = note_key(note)
            record = index.get(key)
            if record is None:
                record = dict(note)
                record["first_seen"] = now
                self.records.append(record)
                index[key] = record
            else:
                for field in TRACE_FIELDS:
                    if note.get(field, "") != record.get(field, ""):
                        record.setdefault("history", []).append(
                            dict(field=field, was=record.get(field, ""),
                                 now=note.get(field, ""))
                        )
                        record[field] = note.get(field, "")
            record["last_seen"] = now
            record["current"] = True
            if fingerprint:
                marks = record.setdefault("ledger_fingerprints", [])
                if fingerprint not in marks:
                    marks.append(fingerprint)
        if fingerprint:
            present = {note_key(note) for note in notes}
            for record in self.records:
                if note_key(record) not in present:
                    record["current"] = False
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.path,
            dict(schema=SCHEMA,
                 updated_at=datetime.now(CHINA).isoformat(timespec="seconds"),
                 notes=self.records),
        )
        return self
