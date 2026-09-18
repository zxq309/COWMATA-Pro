import hashlib
import re
import subprocess

import pytest
from test_dahua_recorded_clock import packet

from cowmata_tailring.workspace import dahua_media as media


@pytest.mark.parametrize(
    "codec,pixel,range_",
    [("hevc", "yuvj420p", "pc"), ("h264", "yuvj420p", "pc"), ("hevc", "yuv420p", "tv")],
)
def test_compatible_camera_video_preserves_codec_and_range(
    monkeypatch, tmp_path, codec, pixel, range_
):
    from test_transcode_fast_395 import setup_media

    module, source, commands = setup_media(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module,
        "probe",
        lambda *a, **k: dict(
            video=dict(codec_name=codec, pix_fmt=pixel, color_range=range_, width=64, height=64)
        ),
    )
    result = module.transcode(source, tmp_path / "out.mp4", 0, 1000)
    assert commands[0][commands[0].index("-c:v") + 1] == "copy"
    assert result["settings"]["video"] == codec
    assert result["settings"]["pixel_format"] == pixel
    assert result["settings"]["color_range"] == range_
    if codec == "hevc":
        assert commands[0][commands[0].index("-tag:v") + 1] == "hvc1"


def test_copy_validation_rejects_changed_video_contract(monkeypatch, tmp_path):
    from test_transcode_fast_395 import setup_media

    module, source, commands = setup_media(monkeypatch, tmp_path)

    def probe(*a, **k):
        return dict(
            video=dict(
                codec_name="hevc" if k.get("dav") else "h264",
                pix_fmt="yuv420p",
                color_range="tv",
                width=64,
                height=64,
            )
        )

    monkeypatch.setattr(module, "probe", probe)
    result = module.transcode(source, tmp_path / "out.mp4", 0, 1000)
    assert result["settings"]["video_processing"] == "encoded"
    assert any("libx264" in command for command in commands)


def test_real_hevc_full_range_keeps_all_pixels_and_frame_times(tmp_path):
    ffmpeg, _ = media.find_ffmpeg()
    raw = tmp_path / "input.hevc"
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
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc",
            "-x265-params",
            "pools=1:frame-threads=1:bframes=0:aud=1:keyint=1:range=full",
            "-f",
            "hevc",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )
    data = raw.read_bytes()
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01\x46\x01", data)]
    assert len(starts) == 75
    source = tmp_path / "hevc-step.dav"
    parts = []
    for i, pos in enumerate(starts):
        payload = data[pos : starts[i + 1] if i + 1 < len(starts) else len(data)]
        ext = bytes([0x80, 0, 40, 30, 0x81, 0, 0x0C, 25])
        item = bytearray(
            packet(i, i * 40, 10 + i // 25 - (2 if i >= 25 else 0), 0xFD, ext + payload)
        )
        item[22] = len(ext)
        parts.append(item)
    source.write_bytes(b"".join(parts))
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    clock = media.recorded_clock(source, media.probe(source, dav=True))
    assert clock and clock["recovery"]["wall_clock_events"]
    target = tmp_path / "out.mp4"
    result = media.transcode(source, target, 0, 3000, timing=clock["recovery"])
    assert result["settings"]["video_processing"] == "stream_copy"
    assert result["settings"]["video"] == "hevc"
    assert result["settings"]["color_range"] == "pc"
    assert result["settings"]["verified_video_frames"] == 75
    assert result["info"]["video"]["codec_tag_string"] == "hvc1"
    assert abs(result["duration_ms"] - 3000) <= 40

    def pixels(path, dav=False):
        cmd = [
            str(ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "2",
            *(["-f", "dhav"] if dav else []),
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "yuvj420p",
            "-f",
            "framemd5",
            "-",
        ]
        output = subprocess.run(cmd, check=True, capture_output=True).stdout.decode()
        return [
            line.rsplit(",", 1)[1].strip()
            for line in output.splitlines()
            if line and not line.startswith("#")
        ]

    assert pixels(source, True) == pixels(target)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha
