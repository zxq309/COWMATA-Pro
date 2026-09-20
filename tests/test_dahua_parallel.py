from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_dahua_incremental_archive import fake_prepared, incremental  # noqa: F401

from cowmata_tailring.workspace import dahua_tasks as tasks


def test_two_views_prepare_together_and_archive_independently(incremental, monkeypatch):  # noqa: F811
    farm, job, index, request, sources = incremental
    barrier = threading.Barrier(2, timeout=3)
    monkeypatch.setattr(tasks, "preparation_workers", lambda: 2, raising=False)
    def prepare(row, *args):
        barrier.wait()
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    result = tasks.organize(request, job)
    assert result["status"] == "completed"
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2
    report = tasks.read_json(job / "dahua-run.json")
    assert {r["owner"] for r in report["records"] if r["status"] == "done"} == {"视角01", "视角02"}
    assert all(p.is_file() for p in sources)


def test_record_timers_and_progress_do_not_overwrite_another_view(tmp_path):
    from cowmata_tailring.workspace.dahua_run import RunLog
    barrier = threading.Barrier(2, timeout=3)
    events = []
    rows = [dict(id=str(i), source=str(i), group=str(i)) for i in (1, 2)]
    log = RunLog(tmp_path, rows, {"1": "视角01", "2": "视角02"}, events.append)
    def work(i):
        log.begin(str(i))
        barrier.wait()
        log.stage("convert", f"view-{i}", frames=i*100)
        barrier.wait()
        snapshot = log.snapshot()
        log.finish("done")
        return snapshot
    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(work, (1, 2)))
    assert [(r["source_id"], r["frames"]) for r in values] == [("1", 100), ("2", 200)]
    assert all(r["status"] == "done" for r in tasks.read_json(tmp_path / "dahua-run.json")["records"])


def test_lazy_chain_reads_only_needed_blocks_and_rechecks_new_reader():
    import struct

    from cowmata_tailring.workspace.dahua_source import DHFSReader
    data = bytearray(4*1024*1024)
    data[32] = 1
    struct.pack_into("<I", data, 48, 1)
    class Counting(io.BytesIO):
        amount = 0
        def read(self, n=-1):
            result = super().read(n)
            self.amount += len(result)
            return result
    reader = DHFSReader.__new__(DHFSReader)
    reader.cancelled = lambda: False
    reader.stream = Counting(data)
    part = dict(descriptors=None, descriptor_blocks={}, desc_offset=0, count=len(data)//32,
                block=512, fragment=2097152)
    assert reader.chain(part, 1) == ([1], 512)
    assert reader.stream.amount <= 65536
    assert reader.chain(part, 1) == ([1], 512)
    assert reader.stream.amount <= 65536
    data[32] = 0
    reader.stream = Counting(data)
    part["descriptor_blocks"] = {}
    with pytest.raises(ValueError, match="首描述符"):
        reader.chain(part, 1)


def test_fast_view_archives_while_slow_view_is_still_preparing(incremental, monkeypatch):  # noqa: F811
    farm, job, index, request, _ = incremental
    monkeypatch.setattr(tasks, "preparation_workers", lambda: 2)
    archived = threading.Event()
    started = threading.Barrier(2, timeout=3)
    first = index["rows"][0]["id"]
    def prepare(row, *args):
        started.wait()
        if row["id"] == first:
            assert archived.wait(3), "The fast view was blocked behind the slow view"
        return fake_prepared(row, *args)
    def on_row(row):
        if row.get("event_kind") == "archive_record" and row.get("status") == "done":
            archived.set()
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    tasks.organize(request, job, on_row=on_row)
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2


def test_parallel_pause_joins_workers_and_resume_keeps_first_output(incremental, monkeypatch):  # noqa: F811
    import time
    farm, job, index, request, _ = incremental
    monkeypatch.setattr(tasks, "preparation_workers", lambda: 2)
    first = index["rows"][0]["id"]
    started = threading.Barrier(2, timeout=3)
    stopped = threading.Event()
    def prepare(row, idx, req, dest, cancelled, stage):
        started.wait()
        if row["id"] != first:
            try:
                deadline = time.monotonic() + 5
                while not cancelled():
                    assert time.monotonic() < deadline, "Peer worker was not cancelled"
                    time.sleep(.01)
                raise InterruptedError("paused")
            finally:
                stopped.set()
        return fake_prepared(row, idx, req, dest, cancelled, stage)
    def on_row(row):
        if row.get("event_kind") == "archive_record" and row.get("status") == "done":
            raise InterruptedError("pause after durable move")
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    with pytest.raises(InterruptedError):
        tasks.organize(request, job, on_row=on_row)
    assert stopped.is_set()
    target = next((farm / "录像").rglob("*.mp4"))
    before = target.stat().st_mtime_ns
    assert not any(r["status"] == "processing" for r in tasks.read_json(job / "dahua-run.json")["records"])
    def resume(row, *args):
        assert row["id"] != first
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", resume)
    tasks.organize(request, job)
    assert target.stat().st_mtime_ns == before
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2


def test_scheduler_interleaves_views_without_reordering_within_view():
    from cowmata_tailring.workspace.dahua_parallel import fair_records
    rows = [dict(group="a", id=i) for i in range(3)] + [dict(group="b", id=i) for i in range(2)]
    assert [(r["group"], r["id"]) for r in fair_records(rows)] == [("a",0),("b",0),("a",1),("b",1),("a",2)]


def test_progress_summary_lists_simultaneous_views():
    from cowmata_tailring.workspace.dahua_run_ui import DahuaRunTables
    table = DahuaRunTables()
    try:
        table.begin()
        for n in (1, 2):
            table.accept(dict(event_kind="task_record",source_id=str(n),owner=f"视角{n:02}",status="processing",targets=[]))
        table.flush()
        assert "处理中 2 路" in table.summary.text()
        assert "视角01 / 视角02" in table.summary.text()
    finally:
        table.close()


def test_default_view_scheduler_starts_all_twenty_channels():
    assert tasks.preparation_workers() == 20


def test_physical_recorder_reads_are_serialized_to_avoid_seek_thrash():
    assert tasks.preparation_workers({"mode": "disk"}) == 1
    assert tasks.preparation_workers({"mode": "files"}) == 20


def test_progress_speed_excludes_unfinished_reading_tasks():
    from cowmata_tailring.workspace.dahua_run_ui import DahuaRunTables

    table = DahuaRunTables()
    try:
        table.begin()
        table.started -= 200
        table.accept(dict(event_kind="task_record", source_id="done", owner="视角01",
                          status="done", targets=[], size=100 * 1048576, file_seconds=10))
        table.accept(dict(event_kind="task_record", source_id="reading", owner="视角02",
                          status="processing", targets=[], size=0, file_seconds=190))
        table.flush()
        assert "10.00 MiB/秒" in table.summary.text()
    finally:
        table.close()
