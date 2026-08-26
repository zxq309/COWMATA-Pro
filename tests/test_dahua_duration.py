from __future__ import annotations

import os
from pathlib import Path

from cowmata_tailring.media.dahua_duration import (
    DahuaPacketSummary,
    DahuaProgramScan,
    DahuaSeekPoint,
    DahuaTimestampPoint,
    build_dahua_duration_index,
    find_next_recording,
    is_dahua_program_stream,
    load_duration_cache,
    parse_ffprobe_packet_summary,
    save_duration_cache,
    scan_dahua_program_stream,
)
from cowmata_tailring.media.dahua_stream import DahuaPlaybackStream


def _write_dahua(path: Path, *, mtime_ns: int) -> None:
    path.write_bytes(b"\x00\x00\x01\xba" + b"\x00" * 32 + b"DHAV")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _packets(
    *,
    count: int,
    frame_duration_ms: float = 1000.0 / 15.0,
    coverage: float = 0.98,
) -> DahuaPacketSummary:
    return DahuaPacketSummary(
        packet_count=count,
        frame_duration_ms=frame_duration_ms,
        maximum_position=int(coverage * 1_000_000),
        packet_coverage=coverage,
        backward_resets=0,
        forward_outliers=0,
    )


def _video_pes(nal_type_byte: int) -> bytes:
    timestamp_header = b"\x80\xc0\x0a" + b"\x00" * 10
    payload = b"\x00\x00\x00\x01" + bytes((nal_type_byte, 1, 0x80, 0, 0, 0))
    body = timestamp_header + payload
    return b"\x00\x00\x01\xe0" + len(body).to_bytes(2, "big") + body


def _pack() -> bytes:
    return b"\x00\x00\x01\xba" + b"\x00" * 12 + b"DHAV" + b"\x00" * 8


def _decode_timestamp(value: bytes) -> int:
    return (
        ((value[0] >> 1) & 0x07) << 30
        | value[1] << 22
        | ((value[2] >> 1) & 0x7F) << 15
        | value[3] << 7
        | ((value[4] >> 1) & 0x7F)
    )


def test_recognises_dahua_program_stream(tmp_path: Path) -> None:
    source = tmp_path / "imou00024.mp4"
    _write_dahua(source, mtime_ns=1_000_000_000)

    ordinary = tmp_path / "ordinary.mp4"
    ordinary.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    assert is_dahua_program_stream(source)
    assert not is_dahua_program_stream(ordinary)


def test_finds_only_exact_next_numbered_recording(tmp_path: Path) -> None:
    current = tmp_path / "imou00024.mp4"
    next_source = tmp_path / "imou00025.mp4"
    _write_dahua(current, mtime_ns=1_000_000_000)
    _write_dahua(next_source, mtime_ns=2_000_000_000)

    assert find_next_recording(current) == next_source.resolve()
    next_source.rename(tmp_path / "imou00026.mp4")
    assert find_next_recording(current) is None


def test_packet_summary_reports_resets_outliers_and_coverage() -> None:
    lines = [
        "pts_time=63.169|dts_time=63.102|duration_time=0.066667|pos=100",
        "pts_time=63.219|dts_time=63.152|duration_time=0.066667|pos=200",
        "pts_time=1.100|dts_time=1.033|duration_time=0.066667|pos=300",
        "pts_time=95446.490|dts_time=95446.423|duration_time=0.066667|pos=800",
        "pts_time=3.122|dts_time=3.055|duration_time=0.066667|pos=900",
    ]

    summary = parse_ffprobe_packet_summary(lines, source_size=1_000)

    assert summary.packet_count == 5
    assert summary.frame_duration_ms == 66.667
    assert summary.maximum_position == 900
    assert summary.packet_coverage == 0.9
    assert summary.backward_resets == 2
    assert summary.forward_outliers == 1


def test_program_stream_scan_finds_frames_keyframes_and_timestamps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "imou00024.mp4"
    source.write_bytes(
        _pack() + _video_pes(0x26) + _pack() + _video_pes(0x02) + _pack() + _video_pes(0x26)
    )

    scan = scan_dahua_program_stream(source)

    assert scan.frame_count == 3
    assert len(scan.keyframes) == 2
    assert len(scan.timestamps) == 3
    assert scan.keyframes[0] == (0, 0)
    assert scan.keyframes[1][0] == 2


def test_seek_index_round_trips_through_cache(tmp_path: Path) -> None:
    current = tmp_path / "imou00024.mp4"
    next_source = tmp_path / "imou00025.mp4"
    start_ns = 1_000_000_000_000
    _write_dahua(current, mtime_ns=start_ns)
    _write_dahua(next_source, mtime_ns=start_ns + 2_000_000_000)
    scan = DahuaProgramScan(
        frame_count=30,
        keyframes=((0, 0), (15, 20)),
        timestamps=(
            (10, 0.0, True, True),
            (30, 15.0, False, False),
        ),
    )
    index = build_dahua_duration_index(
        current,
        _packets(count=30),
        scan,
    )
    cache = tmp_path / "cache"

    save_duration_cache(cache, index)
    loaded = load_duration_cache(cache, current)

    assert loaded == index
    assert loaded is not None
    assert loaded.has_seek_index
    assert loaded.seek_point_at_or_before(1_500).byte_offset == 20


def test_timestamp_index_clamps_damaged_pes_clock_regression(
    tmp_path: Path,
) -> None:
    current = tmp_path / "imou00024.mp4"
    _write_dahua(current, mtime_ns=1_000_000_000)
    scan = DahuaProgramScan(
        frame_count=30,
        keyframes=((0, 0),),
        timestamps=(
            (10, 5.0, False, False),
            (20, 4.0, True, True),
            (30, 6.0, True, True),
        ),
    )
    index = build_dahua_duration_index(
        current,
        _packets(count=30),
        scan,
    )
    cache = tmp_path / "cache"

    save_duration_cache(cache, index)
    loaded = load_duration_cache(cache, current)

    assert loaded is not None
    assert [point.time_ms for point in loaded.timestamp_points] == [
        333,
        333,
        400,
    ]


def test_playback_stream_rewrites_pts_and_maps_public_clock(
    tmp_path: Path,
) -> None:
    source = tmp_path / "imou00024.mp4"
    source.write_bytes(b"\x00" * 128)
    stat = source.stat()
    index = build_dahua_duration_index(
        source,
        _packets(count=30),
    )
    index = type(index)(
        source_path=str(source.resolve()),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        duration_ms=2_000,
        basis=index.basis,
        next_path=None,
        next_size=None,
        next_mtime_ns=None,
        packets=index.packets,
        frame_count=30,
        seek_points=(DahuaSeekPoint(0, 0),),
        timestamp_points=(DahuaTimestampPoint(1_000, 20, True, True),),
    )
    stream = DahuaPlaybackStream(index, 500)
    payload = stream.patch_bytes(16, b"\x00" * 16)

    assert _decode_timestamp(payload[4:9]) > 90_000
    assert _decode_timestamp(payload[9:14]) >= 90_000
    assert stream.public_time_ms(stream.ready_raw_time_ms) == 500
    stream.close()


def test_adjacent_start_time_wins_when_packet_scan_matches(
    tmp_path: Path,
) -> None:
    current = tmp_path / "imou00024.mp4"
    next_source = tmp_path / "imou00025.mp4"
    start_ns = 1_000_000_000_000
    _write_dahua(current, mtime_ns=start_ns)
    _write_dahua(
        next_source,
        mtime_ns=start_ns + 1_988_000_000_000,
    )

    index = build_dahua_duration_index(
        current,
        _packets(count=29_813),
    )

    assert index.duration_ms == 1_988_000
    assert index.basis == "adjacent_file_validated"
    assert index.next_path == str(next_source.resolve())


def test_adjacent_start_time_wins_when_ffprobe_stops_early(
    tmp_path: Path,
) -> None:
    current = tmp_path / "imou00023.mp4"
    next_source = tmp_path / "imou00024.mp4"
    start_ns = 1_000_000_000_000
    _write_dahua(current, mtime_ns=start_ns)
    _write_dahua(
        next_source,
        mtime_ns=start_ns + 1_878_000_000_000,
    )

    index = build_dahua_duration_index(
        current,
        _packets(count=21_152, coverage=0.75),
    )

    assert index.duration_ms == 1_878_000
    assert index.basis == "adjacent_file"


def test_complete_packet_scan_rejects_a_large_inter_file_gap(
    tmp_path: Path,
) -> None:
    current = tmp_path / "imou00024.mp4"
    next_source = tmp_path / "imou00025.mp4"
    start_ns = 1_000_000_000_000
    _write_dahua(current, mtime_ns=start_ns)
    _write_dahua(
        next_source,
        mtime_ns=start_ns + 3_600_000_000_000,
    )

    index = build_dahua_duration_index(
        current,
        _packets(count=29_813),
    )

    assert index.duration_ms == 1_987_533
    assert index.basis == "packet_scan"


def test_packet_scan_is_used_for_the_last_recording(tmp_path: Path) -> None:
    source = tmp_path / "imou00156.mp4"
    _write_dahua(source, mtime_ns=1_000_000_000_000)

    index = build_dahua_duration_index(
        source,
        _packets(count=30_000),
    )

    assert index.duration_ms == 2_000_000
    assert index.basis == "packet_scan"
    assert index.next_path is None


def test_cache_is_invalidated_when_next_recording_changes(
    tmp_path: Path,
) -> None:
    current = tmp_path / "imou00024.mp4"
    next_source = tmp_path / "imou00025.mp4"
    start_ns = 1_000_000_000_000
    _write_dahua(current, mtime_ns=start_ns)
    _write_dahua(
        next_source,
        mtime_ns=start_ns + 1_988_000_000_000,
    )
    index = build_dahua_duration_index(
        current,
        _packets(count=29_813),
    )
    cache = tmp_path / "cache"
    save_duration_cache(cache, index)

    assert load_duration_cache(cache, current) == index

    changed_ns = start_ns + 1_990_000_000_000
    os.utime(next_source, ns=(changed_ns, changed_ns))
    assert load_duration_cache(cache, current) is None
