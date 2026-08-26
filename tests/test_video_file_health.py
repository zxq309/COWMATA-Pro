from __future__ import annotations

import os
from pathlib import Path

from cowmata_tailring.app.mixins.release_hardening import (
    ReleaseHardeningMixin,
)
from cowmata_tailring.media.video_file_health import (
    EMPTY_VIDEO_FILE,
    ZERO_FILLED_VIDEO_PLACEHOLDER,
    neighboring_playable_video_paths,
    video_placeholder_issue,
)


def _write_video_header(path: Path) -> None:
    path.write_bytes(b"\x00\x00\x01\xbaDHAV" + b"\x00" * 32)


def test_zero_filled_and_empty_files_are_placeholders(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    zero_filled = tmp_path / "zero.mp4"
    zero_filled.write_bytes(b"\x00" * 131_072)

    assert video_placeholder_issue(empty) == EMPTY_VIDEO_FILE
    assert (
        video_placeholder_issue(zero_filled)
        == ZERO_FILLED_VIDEO_PLACEHOLDER
    )


def test_nonzero_sample_prevents_false_placeholder_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "late-data.mp4"
    payload = bytearray(131_072)
    payload[len(payload) // 2] = 1
    source.write_bytes(payload)

    assert video_placeholder_issue(source) is None


def test_placeholder_cache_uses_file_identity(tmp_path: Path) -> None:
    source = tmp_path / "changed.mp4"
    source.write_bytes(b"\x00" * 131_072)
    first_mtime = source.stat().st_mtime_ns
    assert video_placeholder_issue(source) == ZERO_FILLED_VIDEO_PLACEHOLDER

    _write_video_header(source)
    changed_mtime = max(source.stat().st_mtime_ns, first_mtime + 1_000_000)
    os.utime(source, ns=(changed_mtime, changed_mtime))

    assert video_placeholder_issue(source) is None


def test_neighbor_navigation_skips_placeholders(tmp_path: Path) -> None:
    first = tmp_path / "video000.mp4"
    empty = tmp_path / "video001.mp4"
    zero_filled = tmp_path / "video002.mp4"
    last = tmp_path / "video003.mp4"
    _write_video_header(first)
    empty.write_bytes(b"")
    zero_filled.write_bytes(b"\x00" * 131_072)
    _write_video_header(last)
    files = [first, empty, zero_filled, last]

    previous, following, previous_skipped, following_skipped = (
        neighboring_playable_video_paths(first, files)
    )
    assert previous is None
    assert following == last
    assert previous_skipped == 0
    assert following_skipped == 2

    previous, following, previous_skipped, following_skipped = (
        neighboring_playable_video_paths(last, files)
    )
    assert previous == first
    assert following is None
    assert previous_skipped == 2
    assert following_skipped == 0


class _Status:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:  # noqa: N802
        self.text = value


class _OpenBase:
    def __init__(self) -> None:
        self.video_status = _Status()
        self.opened: list[str] = []
        self.errors: list[str] = []

    def open_video(self, path: str | None = None) -> None:
        if path:
            self.opened.append(path)

    def _show_error(self, message: str) -> None:
        self.errors.append(message)


class _OpenHarness(ReleaseHardeningMixin, _OpenBase):
    pass


def test_release_open_rejects_placeholder_before_player(
    tmp_path: Path,
) -> None:
    source = tmp_path / "zero.mp4"
    source.write_bytes(b"\x00" * 131_072)
    instance = _OpenHarness()

    instance.open_video(str(source))

    assert instance.opened == []
    assert instance.errors
    assert "zero.mp4" in instance.video_status.text
