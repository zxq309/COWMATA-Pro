from __future__ import annotations

import hashlib
import io
import struct
from datetime import datetime
from pathlib import Path

import pytest

from cowmata_tailring.workspace.dahua_source import (
    TZ,
    DHFSReader,
    normalize_chunks,
    normalize_file,
    packed_ms,
)


def stamp(y=2026, month=9, day=13, h=17, m=49, s=40):
    return ((y - 2000) << 26) | (month << 22) | (day << 17) | (h << 12) | (m << 6) | s


def packet(payload=b"x", kind=0xFD, when=None):
    header = bytearray(24)
    header[:4] = b"DHAV"
    header[4] = kind
    length = 32 + len(payload)
    struct.pack_into("<I", header, 12, length)
    struct.pack_into("<I", header, 16, stamp() if when is None else when)
    return bytes(header) + payload + b"dhav" + struct.pack("<I", length)


def test_time_is_beijing_once():
    expected = int(datetime(2026, 9, 13, 17, 49, 40, tzinfo=TZ).timestamp() * 1000)
    assert packed_ms(stamp()) == expected


def test_dhii_cross_fragment_keeps_first_keyframe_and_small_metadata(tmp_path):
    first = packet(b"a" * 5000)
    tiny = packet(b"", kind=0xF1, when=0)
    index = bytearray(100)
    index[:4] = b"DHII"
    struct.pack_into("<III", index, 64, len(index), len(first), stamp())
    # Stale bytes before the indexed start must never be read as frames.
    index[80:84] = b"DHAV"
    raw = bytes(index) + first + tiny + b"\xff" * 91
    target = tmp_path / "stream.dav"
    result = normalize_chunks((raw[:130], raw[130:2500], raw[2500:]), target)
    assert target.read_bytes() == first + tiny
    assert result["packets"] == 2
    assert result["sha256"] == hashlib.sha256(first + tiny).hexdigest()


@pytest.mark.parametrize(
    "bad",
    [b"garbage", packet()[:-1], packet() + b"unknown", packet()[:-8] + b"bad!" + packet()[-4:]],
)
def test_invalid_packets_fail_closed(tmp_path, bad):
    with pytest.raises(ValueError):
        normalize_chunks([bad], tmp_path / "bad.dav")


def test_dhii_bad_first_frame_does_not_carve_later_frames(tmp_path):
    index = bytearray(100)
    index[:4] = b"DHII"
    struct.pack_into("<III", index, 64, 100, 333, stamp())
    with pytest.raises(ValueError, match="首帧"):
        normalize_chunks([bytes(index) + packet()], tmp_path / "bad.dav")


def test_preserves_source_and_refuses_output_overwrite(tmp_path):
    source, target = tmp_path / "source.dav", tmp_path / "target.dav"
    source.write_bytes(packet())
    before = source.read_bytes()
    normalize_file(source, target)
    assert source.read_bytes() == before
    with pytest.raises(FileExistsError):
        normalize_file(source, target)


def test_cancel_does_not_touch_source(tmp_path):
    source = tmp_path / "source.dav"
    source.write_bytes(packet())
    with pytest.raises(InterruptedError):
        normalize_file(source, tmp_path / "out.dav", lambda: True)
    assert source.read_bytes() == packet()


def test_invalid_disk_magic_and_bounds(tmp_path):
    source = tmp_path / "image.bin"
    source.write_bytes(bytes(20000))
    with pytest.raises(ValueError, match="DHFS"):
        DHFSReader(source, 20000, "id")


def test_chain_validates_reciprocal_links():
    reader = DHFSReader.__new__(DHFSReader)
    reader.cancelled = lambda: False
    data = bytearray(96)
    data[32], data[33] = 1, 62
    struct.pack_into("<H", data, 34, 1)
    struct.pack_into("<I", data, 44, 2)
    struct.pack_into("<I", data, 48, 2)
    data[64], data[65] = 2, 62
    struct.pack_into("<H", data, 66, 1)
    struct.pack_into("<I", data, 84, 1)
    struct.pack_into("<I", data, 88, 1)
    part = dict(descriptors=bytes(data), count=3, block=512, fragment=2097152)
    assert reader.chain(part, 1) == ([1, 2], 1024)
    data[84:88] = bytes(4)
    part["descriptors"] = bytes(data)
    with pytest.raises(ValueError, match="链接"):
        reader.chain(part, 1)


def test_physical_reads_are_sector_aligned():
    from cowmata_tailring.workspace.dahua_source import _read_at

    class SectorStream(io.BytesIO):
        def seek(self, offset, whence=0):
            assert offset % 512 == 0
            return super().seek(offset, whence)

        def read(self, count=-1):
            assert count % 512 == 0
            return super().read(count)

    data = bytes(range(256)) * 20
    assert _read_at(SectorStream(data), 0x234, 64) == data[0x234:0x274]


@pytest.mark.parametrize(
    "lo,hi,expected",
    [
        ("2026-09-13 23:59:59", "2026-09-14 00:00:01", 2),
        ("2026-09-13 10:00:00", "2026-09-13 11:00:00", 1),
    ],
)
def test_strict_time_range_and_midnight_split(lo, hi, expected):
    from cowmata_tailring.workspace.dahua_media import segments, time_ms

    result = segments(time_ms(lo) - 10000, time_ms(hi) + 10000, lo, hi)
    assert len(result) == expected
    assert result[0][0] == time_ms(lo) and result[-1][1] == time_ms(hi)
    assert segments(time_ms(hi), time_ms(hi) + 10000, lo, hi) == []


def test_bad_range_is_rejected():
    from cowmata_tailring.workspace.dahua_media import segments

    with pytest.raises(ValueError):
        segments(1, 2000, 100, 99)


def test_twenty_slot_planner_preserves_numbers_and_conflicts(tmp_path):
    from cowmata_tailring.workspace.dahua_media import time_ms
    from cowmata_tailring.workspace.dahua_tasks import video_row

    source = tmp_path / "prepared.mp4"
    source.write_bytes(b"mp4 verified by preparation")
    target = tmp_path / "farm"
    prepared = dict(
        path=str(source),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        start_ms=time_ms("2026-09-13 10:00:00"),
        duration_ms=1000,
        metadata={},
    )
    row = video_row(prepared, "视角20", target, {})
    assert row["owner"] == "视角20"
    assert (
        Path(row["target"]).relative_to(target).as_posix()
        == "Video/2026-09-13/视角20/2026-09-13_10-00-00.mp4"
    )
    assert row["metadata"]["camera"] == "视角20"
    existing = Path(row["target"])
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"different")
    assert video_row(prepared, "视角20", target, {})["target"].endswith("__001.mp4")
    with pytest.raises(ValueError):
        video_row(prepared, "视角21", target, {})


def test_dahua_commit_reuses_covered_lease_and_has_separate_checkpoint(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import resource_import as imports
    from cowmata_tailring.workspace.dahua_tasks import video_row
    from cowmata_tailring.workspace.dataset_access import DatasetLease

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "locks"))
    job = tmp_path / "job"
    source = job / "records/one/prepared.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture commit only")
    root = tmp_path / "farm/产犊"
    token = "a" * 32
    prepared = dict(
        path=str(source),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        start_ms=1789272000000,
        duration_ms=1000,
        metadata={},
    )
    row = video_row(prepared, "视角20", root, {})
    plan = dict(
        adapter="cowmata-dahua-1",
        mode="import",
        farm="farm",
        id=token,
        target=str(root),
        sources=[dict(path=str(job / "records"), kind="video")],
        rows=[row],
        category="calving",
        start=row["record_date"],
        end=row["record_date"],
        created_at="test",
        fast_video=True,
        delete_unusable=False,
        scenario="mixed",
    )
    with DatasetLease([root, job / "records"], "organize", owner=token) as lease:
        result = imports.execute(plan, job, _lease=lease)
        assert result["completed"]
        assert lease.file_lock is not None
    assert Path(row["target"]).read_bytes() == source.read_bytes()
    assert (job / "dahua-commit.json").is_file() and not (job / "plan.json").exists()
    assert len(list((root / "Video" / row["record_date"]).iterdir())) == 20


def test_new_page_is_independent_from_legacy_tasks(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.organization_ui import OrganizationWindow

    _app = QApplication.instance() or QApplication([])
    window = OrganizationWindow(None)
    assert window.mode_sheets.count() == 2
    panel = window.dahua_panel
    assert len(panel.mapping) == 20 and all(c.currentData() == "" for c in panel.mapping)
    assert panel.job is None and panel.process is None
    assert window.mode_sheets.currentIndex() == 0
    assert panel.midnight.isChecked()
    assert window.request_shutdown()
    window.close()


def test_worker_closes_shared_slot_on_success(tmp_path, monkeypatch):
    import json

    from cowmata_tailring.workspace import classification_resources, dahua_worker
    from cowmata_tailring.workspace.storage import ProjectLock, atomic_json

    job = tmp_path / "job"
    job.mkdir()
    atomic_json(job / "dahua-request.json", dict(action="previews", groups=[]))
    atomic_json(job / "dahua-index.json", dict(rows=[]))
    slot = ProjectLock(tmp_path / "slot.lock")
    monkeypatch.setattr(classification_resources, "limit_worker", lambda *a, **k: {})
    monkeypatch.setattr(classification_resources, "acquire_preparation_slot", lambda *a: slot)
    monkeypatch.setattr(dahua_worker.tasks, "disks", lambda: [])
    monkeypatch.setattr(dahua_worker.sys, "argv", ["worker", str(job)])
    assert dahua_worker.main() == 0
    assert slot.file.closed
    assert json.loads((job / "dahua-result.json").read_text()) == {"previews": []}
