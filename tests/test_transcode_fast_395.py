from pathlib import Path
from types import SimpleNamespace

import pytest


def setup_media(monkeypatch, tmp_path, fail_copy=False):
    from cowmata_tailring.workspace import dahua_media as media

    commands = []
    monkeypatch.setattr(media, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        media,
        "probe",
        lambda *a, **k: dict(video=dict(codec_name="h264", pix_fmt="yuv420p", width=64, height=64)),
    )
    monkeypatch.setattr(
        media,
        "probe_media_timeline",
        lambda *a, **k: SimpleNamespace(duration_ms=1000, to_dict=lambda: dict(duration_ms=1000)),
    )

    def run(args, *a):
        args = list(map(str, args))
        commands.append(args)
        if args[-1].endswith(".mp4"):
            Path(args[-1]).write_bytes(b"task-owned output")
        if fail_copy and "copy" in args:
            raise ValueError("unsupported stream timestamps")
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(media, "run", run)
    source = tmp_path / "source.dav"
    source.write_bytes(b"original")
    return media, source, commands


def test_compatible_start_aligned_h264_uses_streamcopy(monkeypatch, tmp_path):
    media, source, commands = setup_media(monkeypatch, tmp_path)
    result = media.transcode(source, tmp_path / "out.mp4", 0, 1000)
    assert "copy" in commands[0]
    assert result["settings"]["video_processing"] == "stream_copy"
    assert source.read_bytes() == b"original"


def test_bad_remux_falls_back_and_keeps_original(monkeypatch, tmp_path):
    media, source, commands = setup_media(monkeypatch, tmp_path, True)
    result = media.transcode(source, tmp_path / "out.mp4", 0, 1000)
    assert any("libx264" in c for c in commands)
    assert result["settings"]["video_processing"] == "encoded"
    assert source.read_bytes() == b"original"


def test_mid_record_cut_uses_exact_encode_and_never_overwrites_existing(monkeypatch, tmp_path):
    media, source, commands = setup_media(monkeypatch, tmp_path)
    target = tmp_path / "out.mp4"
    media.transcode(source, target, 500, 1000)
    assert "libx264" in commands[0]
    with pytest.raises(FileExistsError):
        media.transcode(source, target, 0, 1000)
    assert target.read_bytes() == b"task-owned output"
