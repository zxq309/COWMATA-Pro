# ruff: noqa: F811 -- pytest fixture injection
import json
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QByteArray
from test_classifier_hotfix import video_row
from test_fixes_v351 import organizer  # noqa: F401


def test_low_memory_reuses_one_heavy_worker_across_views(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import intake_health
    from cowmata_tailring.workspace import video_intake as v

    monkeypatch.setattr(
        v,
        "resource_snapshot",
        lambda: dict(logical_cpus=12, available_bytes=2 * 1024**3, total_bytes=16 * 1024**3),
        raising=False,
    )
    monkeypatch.setattr(v, "inspect", video_row)
    active = maximum = 0
    identities = set()
    lock = threading.Lock()

    def health(path, cache, cancelled=lambda: False):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            identities.add(threading.get_ident())
        time.sleep(0.1)
        with lock:
            active -= 1
        return dict(stamp=v.core.file_stamp(path), delete_reason="")

    monkeypatch.setattr(intake_health, "assess_video", health)
    sources = []
    for i in range(4):
        path = tmp_path / f"cam{i}" / "a.mp4"
        path.parent.mkdir()
        path.write_bytes(bytes([i + 1]) * 1024)
        sources.append(dict(kind="video", path=str(path.parent), camera=f"视角0{i + 1}"))
    result = v.organize(
        tmp_path / "farm",
        sources,
        category="calving",
        farm=str(tmp_path / "farm"),
        job=tmp_path / "job",
        cache=tmp_path / "cache",
        delete_unusable=True,
        workers=8,
    )
    assert result["archived_files"] == 4
    assert maximum <= 1 and len(identities) == 1, (
        "Heavy decoding/OCR workers grow with camera count"
    )


def test_byte_progress_is_coalesced_without_recounting_whole_table(
    organizer, tmp_path, monkeypatch
):  # noqa: F811
    calls = []
    monkeypatch.setattr(organizer, "refresh_view_progress", lambda rows: calls.append(len(rows)))
    messages = b"".join(
        (
            json.dumps(
                dict(
                    event="progress",
                    unit="bytes",
                    current=i,
                    total=100,
                    path="快速复制 · 视角01 · a.mp4",
                )
            )
            + "\n"
        ).encode()
        for i in range(1, 101)
    )

    class Process:
        def readAllStandardOutput(self):
            return QByteArray(messages)

    organizer.process = Process()
    organizer._job_action = "organize"
    try:
        organizer.read_output()
        assert len(calls) <= 1, "Each data block triggers a full table scan in the GUI thread"
    finally:
        organizer.process = None


def test_copy_batch_has_a_small_memory_ceiling(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.fast_transfer import copy_verified

    source, target = tmp_path / "source", tmp_path / "partial"
    with source.open("wb") as out:
        for _ in range(20):
            out.write(b"x" * (1024 * 1024))
    real_open = Path.open
    queued = maximum = 0

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, k):
            return getattr(self.stream, k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.stream.close()

        def read(self, n=-1):
            nonlocal queued, maximum
            data = self.stream.read(n)
            queued += len(data)
            maximum = max(maximum, queued)
            return data

    class Writer(Reader):
        def write(self, data):
            nonlocal queued
            queued = 0
            return self.stream.write(data)

    def opened(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == source and mode == "rb":
            return Reader(stream)
        if path == target and mode in {"xb", "r+b"}:
            return Writer(stream)
        return stream

    monkeypatch.setattr(Path, "open", opened)
    stats = copy_verified(source, target, None, policy="bulk", block_size=512 * 1024)
    assert stats["written_bytes"] == 20 * 1024**2
    assert maximum <= 8 * 1024**2, "Every view retains a large copy batch in RAM"


def test_health_decoder_logs_are_streamed_and_filters_are_bounded(tmp_path, monkeypatch):
    from cowmata_tailring.media import subprocess_tools
    from cowmata_tailring.workspace.intake_health import assess_video

    source = tmp_path / "a.mp4"
    source.write_bytes(b"nonzero recording header")

    def runner(command, **options):
        assert "-filter_threads" in command and command[command.index("-filter_threads") + 1] == "1"
        assert options.get("stdout_file") is not None and options.get("stderr_file") is not None
        options["stdout_file"].write(b"frame=1\n")
        options["stderr_file"].write(b"[Parsed_blackframe_1] frame:0 pblack:100 pts:0 t:0\n")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess_tools, "run_cancellable", runner)
    result = assess_video(source, tmp_path / "cache")
    assert result["delete_reason"] and source.exists()
