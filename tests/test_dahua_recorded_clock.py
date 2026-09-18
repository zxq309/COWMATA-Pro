import struct
from datetime import datetime

import pytest

from cowmata_tailring.workspace import dahua_media as media
from cowmata_tailring.workspace.dahua_source import TZ


def packet(frame, tick, second, kind=0xFD, payload=b"frame"):
    dt = datetime(2026, 9, 13, 18, 0, second)
    packed = (
        ((dt.year - 2000) << 26)
        | (dt.month << 22)
        | (dt.day << 17)
        | (dt.hour << 12)
        | (dt.minute << 6)
        | dt.second
    )
    length = 24 + len(payload) + 8
    head = bytearray(24)
    head[:4] = b"DHAV"
    head[4] = kind
    struct.pack_into("<IIIH", head, 8, frame, length, packed, tick % 65536)
    return bytes(head) + payload + b"dhav" + struct.pack("<I", length)


def get_counter(path):
    function = getattr(media, "recorded_clock", None)
    assert function is not None, "No validation of the recorder's continuous millisecond counter"
    return function(path, dict(video=dict(has_b_frames=0), streams=[]))


def test_recorded_counter_handles_wrap_and_wall_clock_step_without_dropping_frames(tmp_path):
    path = tmp_path / "step.dav"
    path.write_bytes(b"".join(packet(i, 65500 + i * 40, 10 if i < 25 else 9) for i in range(40)))
    clock = get_counter(path)
    assert clock["duration"] == 1.6
    assert clock["packets"] == 40
    assert clock["recovery"]["frame_interval_ms"] == 40
    assert clock["recovery"]["wall_clock_events"][0]["media_ms"] == 1000
    assert clock["recovery"]["wall_clock_events"][0]["wall_ms"] == int(
        datetime(2026, 9, 13, 18, 0, 9, tzinfo=TZ).timestamp() * 1000
    )


@pytest.mark.parametrize(
    "numbers,ticks", [([0, 1, 3], [0, 40, 80]), ([0, 1, 2], [0, 40, 120]), ([0, 1, 2], [0, 40, 20])]
)
def test_unproved_or_missing_frame_timing_never_gets_a_guessed_repair(tmp_path, numbers, ticks):
    path = tmp_path / "gap.dav"
    path.write_bytes(b"".join(packet(n, t, 10) for n, t in zip(numbers, ticks)))
    assert get_counter(path) is None


def test_counter_keeps_audio_offset_and_requires_continuous_audio_samples(tmp_path):
    path = tmp_path / "audio.dav"
    path.write_bytes(
        b"".join(
            packet(i, i * 40, 10) + packet(i, i * 40 + 40, 10, 0xF0, bytes(320)) for i in range(5)
        )
    )
    function = getattr(media, "recorded_clock", None)
    assert function is not None
    info = dict(
        video=dict(has_b_frames=0),
        streams=[dict(codec_type="audio", codec_name="pcm_alaw", sample_rate="8000", channels=1)],
    )
    result = function(path, info)
    assert result["recovery"]["audio_offset_ms"] == 40
    changed = path.read_bytes().replace(
        packet(2, 120, 10, 0xF0, bytes(320)), packet(4, 120, 10, 0xF0, bytes(320))
    )
    path.write_bytes(changed)
    assert function(path, info) is None


def test_conversion_preserves_measured_frames_and_audio_offset(tmp_path, monkeypatch):
    from types import SimpleNamespace

    commands = []
    monkeypatch.setattr(media, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        media,
        "probe",
        lambda *a, **k: dict(video=dict(codec_name="h264", pix_fmt="yuv420p"), format={}),
    )

    def run(command, *a, **k):
        commands.append(command)
        return SimpleNamespace(stdout=b"frame=50\nprogress=end\n", stderr=b"")

    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(
        media,
        "probe_media_timeline",
        lambda *a, **k: SimpleNamespace(duration_ms=2000, to_dict=lambda: {}),
    )
    timing = dict(frame_interval_ms=40, video_frames=100, audio_offset_ms=40)
    result = media._encode("sample.dav", tmp_path / "out.mp4", 1000, 2000, timing=timing)
    assert result["settings"]["verified_video_frames"] == 50
    assert "setpts=N*40/(1000*TB)" in commands[0][commands[0].index("-vf") + 1]
    assert commands[0][commands[0].index("-af") + 1] == "asetpts=N/SR/TB+40/(1000*TB)"
    assert commands[0][commands[0].index("-fps_mode") + 1] == "passthrough"
    assert "-progress" in commands[1]
    monkeypatch.setattr(
        media,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=b"frame=49\nprogress=end\n", stderr=b""),
    )
    with pytest.raises(ValueError, match="帧数"):
        media._encode("sample.dav", tmp_path / "out.mp4", 1000, 2000, timing=timing)


def test_preparation_uses_validated_counter_and_preserves_clock_evidence(tmp_path, monkeypatch):
    import hashlib

    from cowmata_tailring.workspace import dahua_tasks as tasks

    source = tmp_path / "source.dav"
    source.write_bytes(b"normalized source")
    timing = dict(
        frame_interval_ms=40,
        video_frames=25,
        audio_offset_ms=None,
        wall_clock_events=[dict(media_ms=400, wall_ms=0, calendar_step_ms=-1000)],
    )
    monkeypatch.setattr(
        tasks,
        "normalized",
        lambda *a: (
            source,
            dict(start_ms=0, sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
        ),
    )
    monkeypatch.setattr(tasks, "probe", lambda *a, **k: dict(video=dict(width=640, height=360)))
    monkeypatch.setattr(
        tasks, "recorded_clock", lambda *a: dict(duration=1, packets=25, recovery=timing)
    )
    monkeypatch.setattr(
        tasks,
        "packet_clock",
        lambda *a, **k: pytest.fail(
            "Repeated strict packet scan rejected a proved continuous counter"
        ),
    )

    def convert(source, target, offset, duration, *a, **k):
        assert k["timing"] == timing
        target.write_bytes(b"verified output")
        return dict(
            info=dict(video=dict(width=640, height=360), format={}),
            duration_ms=1000,
            timeline=dict(source={}, firstPtsMs=0),
            settings={},
        )

    monkeypatch.setattr(tasks, "transcode", convert)
    result = tasks.prepare_record(
        dict(id="source", source="disk", source_identity="identity"),
        {},
        {},
        tmp_path,
        lambda: False,
    )[0]
    assert result["metadata"]["dahua"]["packet_clock"]["recovery"] == timing
    assert result["metadata"]["needs_review"]
    assert any("校时" in w for w in result["metadata"]["warnings"])
    assert result["options"]["profile"] != "h264-vfr-crf23-v2"


def test_h264_counter_repair_keeps_fast_copy_and_all_frames(tmp_path):
    import re
    import subprocess

    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg

    ffmpeg, _ = find_ffmpeg()
    raw = tmp_path / "frames.h264"
    subprocess.run(
        [
            str(ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=25",
            "-frames:v",
            "75",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-x264-params",
            "aud=1:keyint=1",
            "-f",
            "h264",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )
    data = raw.read_bytes()
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01\x09", data)]
    assert len(starts) == 75
    source = tmp_path / "h264-calendar-step.dav"
    packets = []
    for i, position in enumerate(starts):
        payload = data[position : starts[i + 1] if i + 1 < len(starts) else len(data)]
        ext = bytes([0x80, 0, 40, 30, 0x81, 0, 2, 25])
        packet_data = bytearray(
            packet(i, i * 40, 10 + i // 25 - (2 if i >= 25 else 0), 0xFD, ext + payload)
        )
        packet_data[22] = len(ext)
        packets.append(packet_data)
    source.write_bytes(b"".join(packets))
    info = media.probe(source, dav=True)
    clock = media.recorded_clock(source, info)
    assert clock and clock["recovery"]["wall_clock_events"]
    result = media.transcode(source, tmp_path / "recovered.mp4", 0, 3000, timing=clock["recovery"])
    assert result["settings"]["video_processing"] == "stream_copy"
    assert result["settings"]["verified_video_frames"] == 75
    assert abs(result["duration_ms"] - 3000) <= 40
