"""Recorder timestamp recovery must preserve every source frame and native offsets."""
from types import SimpleNamespace

import pytest
from test_dahua_recorded_clock import packet

from cowmata_tailring.workspace import dahua_media as media


def camera_info():
    return dict(video=dict(has_b_frames=0, r_frame_rate="25/1"),
                streams=[dict(codec_type="audio", codec_name="pcm_alaw", sample_rate="8000", channels=1)])


def jittered_source(path, *, missing=False):
    parts = []
    for i in range(75):
        video_tick = i * 40 + (40 if i >= 25 else 0) - (20 if i >= 50 else 0)
        audio_tick = i * 40 + 10 + (40 if i >= 25 else 0) - (40 if i >= 40 else 0)
        parts.append(packet(i, video_tick, 10 + i // 25))
        parts.append(packet(i + (1 if missing and i >= 40 else 0), audio_tick, 10 + i // 25, 0xF0, bytes(320)))
    path.write_bytes(b"".join(parts))
    return path


def test_native_video_jitter_and_duplicate_audio_tick_are_verified_not_guessed(tmp_path):
    clock = media.recorded_clock(jittered_source(tmp_path / "audio.dav"), camera_info())
    assert clock is not None
    assert clock["duration"] == 3.02
    timing = clock["recovery"]
    assert timing["video_frames"] == 75
    assert timing["audio_offset_ms"] == 10
    assert timing["audio_samples"] == 75 * 320
    assert timing["video_clock_corrections"] == [[25, 40], [50, -20]]
    assert media.expected_video_frames(timing, 0, 1000) == 25
    assert media.expected_video_frames(timing, 1000, 1000) == 24


def test_missing_audio_packet_is_never_repaired_as_continuous_sound(tmp_path):
    assert media.recorded_clock(jittered_source(tmp_path / "gap.dav", missing=True), camera_info()) is None


def test_pts_statistics_cannot_certify_original_frame_or_audio_timing(monkeypatch):
    monkeypatch.setattr(media, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    pts = b"\n".join(f"pts_time={i*.04:.3f}|duration_time=0.040".encode() for i in range(100))
    monkeypatch.setattr(media, "run", lambda *a, **k: SimpleNamespace(stdout=pts))
    clock = media.packet_clock("source.dav", dav=True)
    assert clock.get("recovery") is None


def test_full_record_remux_does_not_trim_on_broken_demuxer_clock(monkeypatch, tmp_path):
    from test_transcode_fast_395 import setup_media
    module, source, commands = setup_media(monkeypatch, tmp_path)
    timing = dict(method="validated_dhav_counter", frame_interval_ms=40,
                  video_frames=75, audio_offset_ms=10, video_clock_corrections=[[25, 40], [50, -20]], duration_ms=3020)
    monkeypatch.setattr(module, "_validate_output", lambda *a, **k: dict(info={}))
    module.transcode(source, tmp_path / "out.mp4", 0, 3020, timing=timing)
    assert "-t" not in commands[0]
    assert "copy" in commands[0]
    bsf = commands[0][commands[0].index("-bsf:v") + 1]
    assert "25" in bsf and "50" in bsf


def test_losing_even_one_verified_frame_is_rejected(monkeypatch, tmp_path):
    from test_transcode_fast_395 import setup_media
    module, source, commands = setup_media(monkeypatch, tmp_path)
    info = module.probe(source)
    monkeypatch.setattr(module, "packet_clock", lambda *a, **k: dict(packets=74))
    timing = dict(method="validated_dhav_counter", frame_interval_ms=40, video_frames=75,
                  audio_offset_ms=None, video_clock_corrections=[[25,40],[50,-20]], duration_ms=3020)
    with pytest.raises(ValueError, match="帧数"):
        module._validate_output(tmp_path / "out.mp4", 3020, SimpleNamespace(stderr=b""), "stream_copy",
                                lambda: False, timing=timing, source_video=info["video"])
