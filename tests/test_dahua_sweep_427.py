"""4.2.7: sequential recorder sweep, bounded pipeline window, stamp-trusted cache."""
from __future__ import annotations

import hashlib
import io
import struct
import threading
import time

import pytest
from test_dahua_incremental_archive import incremental  # noqa: F401

from cowmata_tailring.workspace import dahua_tasks as tasks
from cowmata_tailring.workspace.dahua_parallel import pipelined_records
from cowmata_tailring.workspace.dahua_source import DHFSReader

FRAGMENT = 8192
VIDEO = 65536


def dhav(payload, when=0):
    header = bytearray(24)
    header[:4] = b"DHAV"
    header[4] = 0xFD
    length = 32 + len(payload)
    struct.pack_into("<I", header, 12, length)
    stamp = (26 << 26) | (9 << 22) | (17 << 17) | (4 << 12) | when
    struct.pack_into("<I", header, 16, stamp)
    return bytes(header) + payload + b"dhav" + struct.pack("<I", length)


class Counting(io.BytesIO):
    def __init__(self, data):
        super().__init__(bytes(data))
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return super().read(n)


def recorder(chains, streams):
    """Synthetic DHFS partition: interleaved chains with valid fragment tails."""
    image = bytearray(VIDEO + 16 * FRAGMENT)
    rows = []
    for channel, chain in enumerate(chains):
        payload = streams[channel]
        body = FRAGMENT - 4096
        pieces = [payload[i:i + body] for i in range(0, len(payload), body)]
        assert len(pieces) == len(chain)
        head = chain[0]
        for position, index in enumerate(chain):
            desc = bytearray(32)
            desc[0] = 1 if position == 0 else 2
            desc[1] = 48 + channel
            last = position == len(chain) - 1
            struct.pack_into("<I", desc, 12, 0 if last else chain[position + 1])
            if position == 0:
                desc[2:4] = (len(chain) - 1).to_bytes(2, "little")
                struct.pack_into("<I", desc, 4, 1)
                struct.pack_into("<I", desc, 8, 2)
                last_size = -(-len(pieces[-1]) // 512) * 512
                struct.pack_into("<I", desc, 16, last_size // 512)
            else:
                desc[2:4] = position.to_bytes(2, "little")
                struct.pack_into("<I", desc, 20, chain[position - 1])
                struct.pack_into("<I", desc, 24, head)
            image[index * 32:(index + 1) * 32] = desc
            offset = VIDEO + index * FRAGMENT
            piece = pieces[position]
            if last:
                piece = piece + b"\xff" * (last_size - len(piece))
                image[offset:offset + len(piece)] = piece
            else:
                image[offset:offset + len(piece)] = piece
                tail = bytearray(4096)
                struct.pack_into("<I", tail, 4, 0x31755713)
                struct.pack_into("<I", tail, 8, 1 if position == 0 else 2)
                struct.pack_into("<I", tail, 28, head)
                image[offset + body:offset + FRAGMENT] = tail
        fingerprint = hashlib.sha256(b"".join(bytes(image[i * 32:(i + 1) * 32]) for i in chain)).hexdigest()
        rows.append(dict(id=hashlib.sha256(f"row{channel}".encode()).hexdigest(), partition=0,
                         descriptor=head, fingerprint=fingerprint, status="indexed",
                         source="disk", source_identity="disk", group=f"channel:{channel + 1}"))
    reader = DHFSReader.__new__(DHFSReader)
    reader.cancelled = lambda: False
    reader.stream = Counting(image)
    reader.partitions = [dict(index=0, descriptors=None, descriptor_blocks={}, desc_offset=0,
                              count=VIDEO // 32, block=512, fragment=FRAGMENT, video=VIDEO)]
    return reader, rows


def streams(sizes):
    return [b"".join(dhav(bytes([65 + n]) * 900, when=i) for i in range(count)) for n, count in enumerate(sizes)]


def test_sweep_delivers_each_row_like_chunks_with_few_coalesced_reads():
    payloads = streams([12, 12])
    reader, rows = recorder([[1, 3, 5], [2, 4, 6]], payloads)
    expected = {row["id"]: b"".join(reader.chunks(row)) for row in rows}
    reader.stream.reads = 0
    got, done = {}, set()
    fallback = reader.sweep(rows, lambda row, item: got.setdefault(row["id"], bytearray()).extend(item), done=done)
    assert fallback == []
    assert done == {row["id"] for row in rows}
    assert {k: bytes(v) for k, v in got.items()} == expected
    assert reader.stream.reads == 1, "interleaved fragments must be read in one ascending pass"


def test_sweep_isolates_a_corrupt_row_and_keeps_the_others():
    payloads = streams([12, 12])
    reader, rows = recorder([[1, 3, 5], [2, 4, 6]], payloads)
    rows[0] = dict(rows[0], fingerprint="0" * 64)
    errors, done = {}, set()

    def deliver(row, item):
        if isinstance(item, BaseException):
            errors[row["id"]] = str(item)
        return True

    reader.sweep(rows, deliver, done=done)
    assert rows[0]["id"] in errors and "变化" in errors[rows[0]["id"]]
    assert done == {rows[1]["id"]}


def test_disk_batch_stages_rows_and_cache_is_trusted_by_stamp(tmp_path, monkeypatch):
    payloads = streams([12, 12])
    reader, rows = recorder([[1, 3, 5], [2, 4, 6]], payloads)
    monkeypatch.setattr(tasks, "fresh_disk", lambda saved: dict(path="x", size=1, identity="disk"))
    monkeypatch.setattr(tasks, "DHFSReader", lambda *a, **k: reader)
    index = dict(mode="disk", disk=dict(identity="disk"), rows=rows)
    job = tmp_path / "job"
    job.mkdir()
    tasks.stage_disk_batch(rows, index, job, lambda: False)
    for row, payload in zip(rows, payloads):
        cached = job / "records" / row["id"][:24] / "normalized.dav"
        assert cached.read_bytes() == payload
    monkeypatch.setattr(tasks, "digest_file", lambda *a, **k: pytest.fail("fresh cache was hashed again"))
    monkeypatch.setattr(tasks, "_check_disk_chain", lambda *a, **k: pytest.fail("fresh cache re-read the disk"))
    source, info = tasks.normalized(rows[0], index, job, lambda: False)
    assert source.read_bytes() == payloads[0]
    assert info["sha256"] == hashlib.sha256(payloads[0]).hexdigest()


def test_batch_pipeline_follows_disk_order_and_bounds_unconsumed_rows():
    rows = [dict(id=f"r{i:02}", partition=0, descriptor=i) for i in range(40)][::-1]
    batches, lock = [], threading.Lock()
    state = dict(staged=0, consumed=0, peak=0)

    def stage_batch(batch, ready=None):
        with lock:
            batches.append([r["id"] for r in batch])
            state["staged"] += len(batch)
            state["peak"] = max(state["peak"], state["staged"] - state["consumed"])

    def finish_one(row):
        return row["id"]

    seen = []
    for _row, future in pipelined_records(rows, None, finish_one, 4, lambda: None, ahead=8,
                                         order_key=lambda r: r["descriptor"],
                                         stage_batch=stage_batch, batch_size=4):
        time.sleep(0.005)  # slow archive consumer
        with lock:
            state["consumed"] += 1
        seen.append(future.result())
    assert sorted(seen) == sorted(r["id"] for r in rows)
    assert [i for batch in batches for i in batch] == [f"r{i:02}" for i in range(40)]
    assert state["peak"] <= 4 + 8 + 1, "staging must not run ahead of the archive consumer"


def test_pipeline_pause_does_not_start_queued_conversions():
    rows = [dict(id=f"r{i:02}", partition=0, descriptor=i) for i in range(20)]
    started, stop = [], threading.Event()

    def finish_one(row):
        started.append(row["id"])
        deadline = time.monotonic() + 2
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise InterruptedError("paused")

    def cancel():
        if stop.is_set():
            raise InterruptedError("paused")

    stream = pipelined_records(rows, lambda row: None, finish_one, 2, cancel, ahead=4)
    thread = threading.Timer(0.2, stop.set)
    thread.start()
    with pytest.raises(InterruptedError):
        for _row, future in stream:
            future.result()
    stream.close()
    assert len(started) <= 2 + 4, "queued conversions started after the pause"


def test_keyframe_pieces_snap_cut_to_nearest_iframe_and_share_the_boundary(tmp_path):
    packets = []
    for frame in range(20):
        body = dhav(b"v" * 40, when=frame % 60)
        body = body[:4] + bytes([0xFD if frame % 5 == 0 else 0xFC]) + body[5:]
        packets.append(body)
        packets.append(body[:4] + bytes([0xF0]) + body[5:])  # interleaved audio
    source = tmp_path / "record.dav"
    source.write_bytes(b"".join(packets))
    timing = dict(frame_interval_ms=40, video_frames=20, video_clock_corrections=[])
    started, ended = 1_000_000, 1_000_800
    cut = started + 7 * 40  # frame 7: nearest keyframe is frame 5
    pieces = tasks.keyframe_pieces(source, timing, [(started, cut), (cut, ended)], started, ended)
    assert [(p["first"], p["end"]) for _lo, _hi, p in pieces] == [(0, 5), (5, 20)]
    assert pieces[0][1] == pieces[1][0] == started + 200
    assert pieces[0][2]["end_pos"] == pieces[1][2]["start_pos"] == 10 * len(packets[0])
    assert pieces[1][2]["end_pos"] == source.stat().st_size


def test_keyframe_pieces_refuse_unprovable_frame_counts(tmp_path):
    source = tmp_path / "record.dav"
    source.write_bytes(dhav(b"v" * 40))
    timing = dict(frame_interval_ms=40, video_frames=99)
    assert tasks.keyframe_pieces(source, timing, [(0, 20), (20, 40)], 0, 40) is None


def test_sweep_hands_each_row_to_ready_exactly_once(tmp_path, monkeypatch):
    payloads = streams([12, 12])
    reader, rows = recorder([[1, 3, 5], [2, 4, 6]], payloads)
    monkeypatch.setattr(tasks, "fresh_disk", lambda saved: dict(path="x", size=1, identity="disk"))
    monkeypatch.setattr(tasks, "DHFSReader", lambda *a, **k: reader)
    index = dict(mode="disk", disk=dict(identity="disk"), rows=rows)
    job = tmp_path / "job"
    job.mkdir()
    ready = []
    tasks.stage_disk_batch(rows, index, job, lambda: False, ready=lambda row: ready.append(row["id"]))
    assert sorted(ready) == sorted(row["id"] for row in rows)
    ready.clear()
    tasks.stage_disk_batch(rows, index, job, lambda: False, ready=lambda row: ready.append(row["id"]))
    assert sorted(ready) == sorted(row["id"] for row in rows), "cached rows are ready at once"


def test_cache_moves_to_scratch_and_stale_media_is_purged(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.dahua_run import (
        configure_storage,
        media_root,
        purge_stale_media,
        scratch_volume,
    )
    farm, job, scratch = tmp_path / "farm", tmp_path / "job", tmp_path / "ssd" / "COWMATA-归类缓存"
    farm.mkdir()
    job.mkdir()
    monkeypatch.setenv("COWMATA_DAHUA_SCRATCH", str(scratch))
    assert scratch_volume(farm) == scratch.resolve()
    monkeypatch.setenv("COWMATA_DAHUA_SCRATCH", "0")
    assert scratch_volume(farm) is None
    legacy = configure_storage(job, farm)
    for name in ("a" * 24, "b" * 24):
        (legacy / "records" / name / "seg").mkdir(parents=True)
        (legacy / "records" / name / "normalized.dav").write_bytes(b"raw")
        (legacy / "records" / name / "seg" / "prepared.mp4").write_bytes(b"mp4")
        (legacy / "records" / name / "source.json").write_text("{}", encoding="utf-8")
    moved = configure_storage(job, farm, scratch=scratch)
    assert moved == (scratch / "原始录像" / job.name).resolve() == media_root(job)
    assert purge_stale_media(legacy, keep=["b" * 64]) == 2
    assert not (legacy / "records" / ("a" * 24) / "normalized.dav").exists()
    assert (legacy / "records" / ("a" * 24) / "source.json").exists()
    assert (legacy / "records" / ("b" * 24) / "seg" / "prepared.mp4").exists()
    other = tmp_path / "other-farm"
    other.mkdir()
    with pytest.raises(ValueError, match="输出牧场已变化"):
        configure_storage(job, other)

def test_helper_timeout_blocks_one_clip_not_the_task(monkeypatch):
    import subprocess

    from cowmata_tailring.workspace import dahua_media as media

    def slow(command, **_kw):
        raise subprocess.TimeoutExpired(command, 12)

    monkeypatch.setattr(media, "run_cancellable", slow)
    with pytest.raises(ValueError, match="超时"):
        media.run(["ffmpeg"], lambda: False, 12)


def test_encoder_probe_timeout_falls_back_instead_of_raising(monkeypatch):
    from cowmata_tailring.workspace import dahua_media as media

    def run(*_a, **_k):
        raise ValueError("媒体处理超时（45 秒），已停止本次尝试")

    monkeypatch.setattr(media, "run", run)
    assert media._probe_encoder("ffmpeg", None, lambda: False) == "libx264"

def test_recorder_bit_errors_are_kept_losslessly_instead_of_reencoded(monkeypatch, tmp_path):
    from test_transcode_fast_395 import setup_media

    media, source, commands = setup_media(monkeypatch, tmp_path)
    calls = []

    def sample(target, duration_ms, cancelled, *, strict=True):
        calls.append(strict)
        if strict:
            raise ValueError("[dec:hevc] Invalid data found when processing input")

    monkeypatch.setattr(media, "_sample_decode", sample)
    result = media.transcode(source, tmp_path / "out.mp4", 0, 1000)
    assert result["settings"]["video_processing"] == "stream_copy"
    assert result["settings"]["source_damage"] == "recorder_bit_errors_preserved"
    assert calls == [True, False]
    assert not any("libx264" in c for c in commands)

def test_undecodable_record_is_retried_once_then_discarded_not_blocked(incremental, monkeypatch):  # noqa: F811
    from test_dahua_incremental_archive import fake_prepared

    farm, job, index, request, _ = incremental
    bad = index["rows"][0]["id"]
    attempts = []

    def prepare(row, *args):
        if row["id"] == bad:
            attempts.append(1)
            raise ValueError("[dec:hevc] Invalid data found when processing input")
        return fake_prepared(row, *args)

    monkeypatch.setattr(tasks, "prepare_record", prepare)
    result = tasks.organize(request, job)
    assert len(attempts) == 2, "one clean retry before discarding"
    assert not result["issues"], "undecodable recordings must not become 待核对"
    report = tasks.read_json(job / "dahua-run.json")
    statuses = {r["source_id"]: r for r in report["records"]}
    assert statuses[bad]["status"] == "skipped" and "已丢弃" in statuses[bad]["message"]
    assert len(list((farm / "录像").rglob("*.mp4"))) == 1
    monkeypatch.setattr(tasks, "prepare_record", lambda *a: pytest.fail("discarded row was retried"))
    tasks.organize(request, job)


def test_disk_errors_are_kept_for_resume_not_discarded(incremental, monkeypatch):  # noqa: F811
    from test_dahua_incremental_archive import fake_prepared

    farm, job, index, request, _ = incremental
    bad = index["rows"][0]["id"]

    def prepare(row, *args):
        if row["id"] == bad:
            raise OSError("暂存磁盘空间不足，任务已保留，可释放空间后继续")
        return fake_prepared(row, *args)

    monkeypatch.setattr(tasks, "prepare_record", prepare)
    result = tasks.organize(request, job)
    assert [i["source"] for i in result["issues"]] == [bad]
    assert bad not in result["completed_records"]


def test_archive_is_video_only_and_audio_never_blocks_the_counter_clock(tmp_path):
    from test_dahua_audio_clock_repair import camera_info, jittered_source

    from cowmata_tailring.workspace import dahua_media as media

    broken_audio = jittered_source(tmp_path / "gap.dav", missing=True)
    assert media.recorded_clock(broken_audio, camera_info()) is None
    clock = media.recorded_clock(broken_audio, camera_info(), ignore_audio=True)
    assert clock["recovery"]["video_frames"] == 75