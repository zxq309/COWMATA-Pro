from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from cowmata_tailring.workspace import dahua_wipe

SECTOR = 4096


class Disk:
    """In-memory raw device that rejects unaligned I/O like a Windows disk handle."""

    def __init__(self, size):
        self.data = bytearray(b"\xab" * size)
        self.position = 0
        self.unaligned = 0

    def CreateFileW(self, *args):
        return 7

    def SetFilePointer(self, handle, low, high, method):
        self.position = (high._obj.value << 32) | (low.value & 0xFFFFFFFF)
        return low.value

    def _io(self, size):
        if self.position % SECTOR or size % SECTOR:
            self.unaligned += 1
            ctypes.set_last_error(87)
            return False
        return True

    def WriteFile(self, handle, payload, size, written, overlapped):
        if not self._io(size):
            return 0
        self.data[self.position:self.position + size] = bytes(payload)[:size]
        written._obj.value = size
        return 1

    def ReadFile(self, handle, buffer, size, read, overlapped):
        if not self._io(size):
            return 0
        ctypes.memmove(buffer, bytes(self.data[self.position:self.position + size]), size)
        read._obj.value = size
        return 1

    def FlushFileBuffers(self, handle):
        return 1

    def CloseHandle(self, handle):
        pass


def run_wipe(monkeypatch, disk, partitions):
    info = dict(number=3, identity="disk-431", dhfs=True, path=r"\\.\PhysicalDrive3", size=len(disk.data),
                model="test", serial="test")

    class Reader:
        def __init__(self, *args, **kwargs):
            descriptors = [bytes(disk.data[p["desc_offset"]:p["desc_offset"] + p["count"] * 32]) for p in partitions]
            self.partitions = [dict(p, descriptors=d) for p, d in zip(partitions, descriptors)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(dahua_wipe, "disk_info", lambda number: info)
    monkeypatch.setattr(dahua_wipe, "ProjectLock", lambda path: SimpleNamespace(acquired=True, close=lambda: None))
    monkeypatch.setattr(dahua_wipe, "DHFSReader", Reader)
    monkeypatch.setattr(dahua_wipe, "_dismount_overlapping", lambda number, jobs: None)
    monkeypatch.setattr(dahua_wipe, "_kernel", lambda: disk)
    monkeypatch.setattr(dahua_wipe.time, "sleep", lambda _: None)
    events = []
    result = dahua_wipe.wipe(3, "disk-431", lambda *a: events.append(a))
    return result, events


def test_unaligned_descriptor_tail_is_wiped_with_sector_aligned_writes(monkeypatch):
    # 1000 descriptors = 32000 bytes: not a multiple of 512 or 4096 (the Errno 87 case).
    disk = Disk(256 * 1024)
    part = dict(index=0, count=1000, desc_offset=8192, video=65536, fragment=16384)
    disk.data[8192:8192 + 32] = bytes([1, 48]) + b"\x00" * 2 + (5).to_bytes(4, "little") + (9).to_bytes(4, "little") + bytes(20)
    result, events = run_wipe(monkeypatch, disk, [part])
    assert disk.unaligned == 0
    assert result["remaining_recordings"] == 0
    assert disk.data[8192:8192 + 32000] == bytes(32000)
    # Bytes sharing the last sector with the table are preserved.
    assert disk.data[8192 + 32000:8192 + 32768] == b"\xab" * 768
    assert disk.data[:8192] == b"\xab" * 8192
    assert disk.data[65536:65536 + 16384] == bytes(16384)
    assert events and events[-1][0] == events[-1][1]


def test_aligned_plan_covers_region_with_whole_blocks():
    assert dahua_wipe.aligned_plan(1048576, 15261248) == (1048576, 15261696, 0, 448)
    assert dahua_wipe.aligned_plan(4100, 10) == (4096, 4096, 4, 4082)
    assert dahua_wipe.aligned_plan(8192, 4096) == (8192, 4096, 0, 0)


def test_only_volumes_overlapping_the_wipe_are_dismounted(monkeypatch):
    dismounted = []
    monkeypatch.setattr(dahua_wipe, "_volume_extents", lambda number: [("G:", 3_983_609_430_016, 4_000_783_008_256),
                                                                        ("H:", 0, 4096)])

    class Kernel:
        def CreateFileW(self, path, *args):
            dismounted.append(path)
            return 9

        def DeviceIoControl(self, *args):
            return 1

        def CloseHandle(self, handle):
            pass

    monkeypatch.setattr(dahua_wipe, "_kernel", lambda: Kernel())
    dahua_wipe._dismount_overlapping(3, [dict(offset=1_048_576, length=15_261_248)])
    assert dismounted == []
    dahua_wipe._dismount_overlapping(3, [dict(offset=0, length=8192)])
    assert dismounted == ["\\\\.\\H:"]


@pytest.fixture
def panel(monkeypatch):
    from cowmata_tailring.workspace import dahua_ui

    panel = dahua_ui.DahuaPanel.__new__(dahua_ui.DahuaPanel)
    panel.active = False
    panel.mode = SimpleNamespace(currentIndex=lambda: 1)
    panel.disk_choice = SimpleNamespace(currentData=lambda: {"number": 3})
    panel.status = SimpleNamespace(setText=lambda text: None)
    panel.started = []
    panel.start = lambda action, request, job=None: panel.started.append((action, request))
    return dahua_ui, panel


def survey(identity="disk-431"):
    return dict(number=3, identity=identity, model="HGST", serial="S", size=4 * 10**12, existing_recordings=12,
                partitions=[{}])


def test_classification_done_then_one_question_then_wipe_directly(panel, monkeypatch):
    dahua_ui, panel = panel
    shown = []
    monkeypatch.setattr(dahua_ui.QMessageBox, "information", lambda *a, **k: shown.append(("info", a[1])))
    monkeypatch.setattr(dahua_ui.QMessageBox, "question",
                        lambda *a, **k: shown.append(("question", a[1])) or dahua_ui.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(dahua_ui.QMessageBox, "warning", lambda *a, **k: pytest.fail("no second confirmation"))
    panel.index_full = dict(disk=dict(identity="disk-431", model="HGST", serial="S", size=4 * 10**12), rows=[{}] * 12)
    dahua_ui.DahuaPanel._offer_wipe_after_organize(
        panel, {"status": "completed", "completed_records": {"a": {"outputs": [1, 2]}}})
    assert [kind for kind, _ in shown] == ["info", "question"]
    assert panel.started == [("wipe_survey", {"number": 3})]
    dahua_ui.DahuaPanel.confirm_wipe(panel, survey())
    assert panel.started[-1] == ("wipe", {"number": 3, "identity": "disk-431"})


def test_post_classification_wipe_refuses_a_different_disk(panel, monkeypatch):
    dahua_ui, panel = panel
    warnings = []
    monkeypatch.setattr(dahua_ui.QMessageBox, "warning", lambda *a, **k: warnings.append(a[1]))
    panel._wipe_expected = "disk-431"
    dahua_ui.DahuaPanel.confirm_wipe(panel, survey("another-disk"))
    assert warnings and not panel.started


def test_wipe_button_asks_exactly_once(panel, monkeypatch):
    dahua_ui, panel = panel
    asked = []
    monkeypatch.setattr(dahua_ui.QMessageBox, "warning",
                        lambda *a, **k: asked.append(a[1]) or dahua_ui.QMessageBox.StandardButton.Yes)
    dahua_ui.DahuaPanel.wipe_confirm(panel)
    dahua_ui.DahuaPanel.confirm_wipe(panel, survey())
    assert asked == ["立即清盘"]
    assert panel.started == [("wipe_survey", {"number": 3}), ("wipe", {"number": 3, "identity": "disk-431"})]


def test_file_source_classification_only_reports_completion(panel, monkeypatch):
    dahua_ui, panel = panel
    shown = []
    panel.mode = SimpleNamespace(currentIndex=lambda: 0)
    panel.index_full = dict(mode="file", rows=[])
    monkeypatch.setattr(dahua_ui.QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    monkeypatch.setattr(dahua_ui.QMessageBox, "question", lambda *a, **k: pytest.fail("no wipe for file sources"))
    dahua_ui.DahuaPanel._offer_wipe_after_organize(panel, {"status": "completed", "completed_records": {}})
    assert shown == ["数据归类完成"] and not panel.started