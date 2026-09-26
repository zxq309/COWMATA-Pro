from __future__ import annotations

from types import SimpleNamespace


def test_wipe_progress_adapter_accepts_worker_progress_contract(monkeypatch):
    """A real wipe must reach the write loop without a callback TypeError."""
    from cowmata_tailring.workspace import dahua_wipe

    info = {
        "number": 3,
        "identity": "disk-430",
        "dhfs": True,
        "letters": [],
        "path": "\\\\.\\PhysicalDrive3",
        "size": 4096,
        "model": "test",
        "serial": "test",
    }

    class FakeLock:
        acquired = True

        def close(self):
            pass

    class FakeReader:
        partitions = [{
            "index": 0,
            "count": 1,
            "desc_offset": 0,
            "video": 32,
            "fragment": 32,
            "descriptors": bytes(32),
        }]

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeKernel:
        def CreateFileW(self, *args):
            return 7

        def SetFilePointer(self, *args):
            return 0

        def WriteFile(self, handle, payload, size, written, overlapped):
            written._obj.value = size
            return 1

        def FlushFileBuffers(self, *args):
            return 1

        def CloseHandle(self, *args):
            pass

    events = []
    monkeypatch.setattr(dahua_wipe, "disk_info", lambda number: info)
    monkeypatch.setattr(dahua_wipe, "ProjectLock", lambda path: FakeLock())
    monkeypatch.setattr(dahua_wipe, "DHFSReader", FakeReader)
    monkeypatch.setattr(dahua_wipe, "_dismount_volumes", lambda letters: None)
    monkeypatch.setattr(dahua_wipe, "_kernel", lambda: FakeKernel())
    monkeypatch.setattr(dahua_wipe.time, "sleep", lambda _: None)

    result = dahua_wipe.wipe(
        3,
        "disk-430",
        lambda current, total, label: events.append((current, total, label)),
    )

    assert result["remaining_recordings"] == 0
    assert events
    assert events[-1][0] == events[-1][1]


def test_completed_organize_offers_wipe_only_for_recorder_disk(monkeypatch):
    from cowmata_tailring.workspace import dahua_ui

    calls = []
    panel = dahua_ui.DahuaPanel.__new__(dahua_ui.DahuaPanel)
    panel.mode = SimpleNamespace(currentIndex=lambda: 1)
    panel.disk_choice = SimpleNamespace(currentData=lambda: {"number": 3})
    panel.wipe_confirm = lambda: calls.append("wipe-confirm")

    monkeypatch.setattr(
        dahua_ui.QMessageBox,
        "question",
        lambda *args, **kwargs: dahua_ui.QMessageBox.StandardButton.Yes,
    )
    dahua_ui.DahuaPanel._offer_wipe_after_organize(panel, {"status": "completed"})

    assert calls == ["wipe-confirm"]
