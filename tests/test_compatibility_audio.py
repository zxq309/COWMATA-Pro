"""Real FFmpeg regression for camera PS audio headers with no sample rate."""
import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cowmata_tailring.media.ffmpeg_tools import FFmpegToolError, find_ffmpeg
from cowmata_tailring.media.native_ps import packets
from cowmata_tailring.media.timeline import probe_media_timeline
from cowmata_tailring.workspace.compatibility import CompatibilityCache


@pytest.fixture
def ps_recording(tmp_path):
    try:
        ffmpeg, ffprobe = find_ffmpeg()
    except FFmpegToolError:
        pytest.skip("Real FFmpeg tools are required for the PS audio regression")
    path = tmp_path / "camera.mpg"
    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=44100", "-t", "3",
                    "-c:v", "mpeg2video", "-bf", "0", "-c:a", "mp2", "-f", "vob", str(path)],
                   check=True, capture_output=True, timeout=15)
    return path, ffmpeg, ffprobe


def stream_info(path, ffprobe):
    result = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(path)],
                            check=True, capture_output=True, timeout=15)
    return json.loads(result.stdout)["streams"]


def video_frames(path, ffmpeg):
    result = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-map", "0:v:0",
                             "-an", "-f", "framemd5", "-"], check=True, capture_output=True, timeout=15)
    return [line.rsplit(",", 1)[-1].strip() for line in result.stdout.decode().splitlines()
            if line and not line.startswith("#")]


def test_bad_audio_does_not_block_lossless_video_cache(ps_recording, tmp_path):
    source, ffmpeg, ffprobe = ps_recording
    data = bytearray(source.read_bytes())
    corrupted = 0
    for start, code, end in packets(data):
        if code == 0xc0:
            begin = start + 9 + data[start + 8]
            data[begin:end] = bytes(end - begin)
            corrupted += 1
    assert corrupted > 0
    source.write_bytes(data)
    audio = next(s for s in stream_info(source, ffprobe) if s["codec_type"] == "audio")
    assert audio["sample_rate"] == "0"  # Same invalid stream field as the customer failure.
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    timeline = probe_media_timeline(source, ffprobe)
    cache = CompatibilityCache(tmp_path / "project")
    result = cache.build("a" * 64, source, {"timeline": timeline.to_dict()})
    assert [s["codec_type"] for s in stream_info(result, ffprobe)] == ["video"]
    assert video_frames(result, ffmpeg) == video_frames(source, ffmpeg)
    assert len(video_frames(result, ffmpeg)) == 75
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert cache.cached("a" * 64) == result
    assert cache.entries["a" * 64]["audio_omitted"]
    assert not list(cache.root.glob("*.building.mkv"))


def test_valid_audio_is_preserved_in_compatibility_cache(ps_recording, tmp_path):
    source, ffmpeg, ffprobe = ps_recording
    timeline = probe_media_timeline(source, ffprobe)
    cache = CompatibilityCache(tmp_path / "project")
    result = cache.build("b" * 64, source, {"timeline": timeline.to_dict()})
    audio = next(s for s in stream_info(result, ffprobe) if s["codec_type"] == "audio")
    assert audio["sample_rate"] == "44100" and audio["codec_name"] == "mp2"
    assert video_frames(result, ffmpeg) == video_frames(source, ffmpeg)
    assert not cache.entries["b" * 64].get("audio_omitted", False)


def test_ready_cache_lookup_stays_responsive_while_another_video_builds(ps_recording, tmp_path, monkeypatch):
    """A GUI cache lookup must not wait for a different full-file FFmpeg job."""
    from cowmata_tailring.workspace import compatibility

    source, _, ffprobe = ps_recording
    metadata = {"timeline": probe_media_timeline(source, ffprobe).to_dict()}
    cache = CompatibilityCache(tmp_path / "project")
    existing = cache.build("a" * 64, source, metadata)
    entered, release = threading.Event(), threading.Event()
    run = compatibility.run_cancellable

    def delayed_remux(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return run(*args, **kwargs)

    monkeypatch.setattr(compatibility, "run_cancellable", delayed_remux)
    with ThreadPoolExecutor(max_workers=2) as pool:
        build = pool.submit(cache.build, "b" * 64, source, metadata)
        try:
            assert entered.wait(3)
            lookup = pool.submit(cache.cached, "a" * 64)
            # Completion, not a particular implementation lock, is the contract.
            result = lookup.result(timeout=.5)
            assert result == existing
        finally:
            release.set()
            build.result(timeout=10)
