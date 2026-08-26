from __future__ import annotations

import os
from pathlib import Path

from cowmata_tailring.media.dahua_segment_clock import (
    dahua_segment_clock_delta_ms,
)
from cowmata_tailring.media.mixins.dual_anchor_precision import (
    DualAnchorPrecisionV2Mixin,
)


def _write_dahua(path: Path, *, mtime_ms: int) -> None:
    path.write_bytes(b"\x00\x00\x01\xba" + b"\x00" * 32 + b"DHAV")
    mtime_ns = mtime_ms * 1_000_000
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_adjacent_dahua_segment_clock_supports_both_directions(
    tmp_path: Path,
) -> None:
    first = tmp_path / "imou00024.mp4"
    second = tmp_path / "imou00025.mp4"
    _write_dahua(first, mtime_ms=1_000_000)
    _write_dahua(second, mtime_ms=2_988_000)

    assert dahua_segment_clock_delta_ms(first, second, 1_988_000) == 1_988_000
    assert (
        dahua_segment_clock_delta_ms(
            second,
            first,
            1_900_000,
            1_988_000,
        )
        == -1_988_000
    )
    assert dahua_segment_clock_delta_ms(second, first, 1_900_000) is None


def test_segment_clock_rejects_copy_time_and_recording_gap(
    tmp_path: Path,
) -> None:
    first = tmp_path / "imou00024.mp4"
    second = tmp_path / "imou00025.mp4"
    _write_dahua(first, mtime_ms=1_000_000)
    _write_dahua(second, mtime_ms=1_002_600)

    assert dahua_segment_clock_delta_ms(first, second, 1_988_000) is None

    _write_dahua(second, mtime_ms=4_600_000)
    assert dahua_segment_clock_delta_ms(first, second, 1_988_000) is None


def test_segment_clock_requires_exact_neighbor_and_dahua_format(
    tmp_path: Path,
) -> None:
    first = tmp_path / "imou00024.mp4"
    skipped = tmp_path / "imou00026.mp4"
    ordinary = tmp_path / "imou00025.mp4"
    _write_dahua(first, mtime_ms=1_000_000)
    _write_dahua(skipped, mtime_ms=2_988_000)
    ordinary.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    os.utime(ordinary, ns=(2_988_000_000_000, 2_988_000_000_000))

    assert dahua_segment_clock_delta_ms(first, skipped, 1_988_000) is None
    assert dahua_segment_clock_delta_ms(first, ordinary, 1_988_000) is None


class _Plot:
    def __init__(self) -> None:
        self.view_range = (100.0, 200.0)

    def set_view(self, start: float, end: float) -> None:
        self.view_range = (start, end)


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, _timeout: int) -> None:  # noqa: N802
        self.messages.append(message)


class _Media:
    def __init__(self, path: Path, duration_ms: int) -> None:
        self.current_path = str(path.resolve())
        self._duration_ms = duration_ms

    def duration_ms(self) -> int:
        return self._duration_ms

    def get_time_ms(self) -> int:
        return 500_000

    def is_playing(self) -> bool:
        return False


class _SwitchBase:
    def open_video(self, path: str | None = None) -> None:
        assert path is not None
        self.media.current_path = str(Path(path).resolve())
        self.video_path = str(Path(path).resolve())
        self._media_primed = True

    def set_playhead(self, value_ms: float, seek_video: bool = True) -> None:
        del seek_video
        self.playhead_ms = float(value_ms)

    def _set_playhead_visual(self, value_ms: float) -> None:
        self.playhead_ms = float(value_ms)

    def _is_user_playing(self) -> bool:
        return False


class _SwitchHarness(DualAnchorPrecisionV2Mixin, _SwitchBase):
    def __init__(self, source: Path, duration_ms: int) -> None:
        self.media = _Media(source, duration_ms)
        self.video_path = str(source.resolve())
        self.data = object()
        self.data_create_time_ms = 10_000_000
        self.data_duration_ms = 5_000_000.0
        self.playhead_ms = 500_000.0
        self.video_start_wall_ms = 10_100_000
        self.align_method = "pin"
        self.plot = _Plot()
        self._restoring_project = False
        self._media_primed = True
        self._timelines_linked = True
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_snapshot: dict[str, object] = {}
        self._source_switch_mapping_committed = False
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._independent_video_selection_active = False
        self._play_requested_after_anchor_confirmation = False
        self._status_bar = _StatusBar()

    def statusBar(self) -> _StatusBar:  # noqa: N802
        return self._status_bar

    def _update_alignment_status(self) -> None:
        pass


def test_video_switch_uses_segment_clock_instead_of_current_playhead(
    tmp_path: Path,
) -> None:
    first = tmp_path / "imou00024.mp4"
    second = tmp_path / "imou00025.mp4"
    _write_dahua(first, mtime_ms=1_000_000)
    _write_dahua(second, mtime_ms=2_988_000)
    instance = _SwitchHarness(first, 1_988_000)

    instance.open_video(str(second))

    assert instance.video_start_wall_ms == 12_088_000
    assert instance.playhead_ms == 2_088_000
    assert instance.align_method == "segment_clock"
    assert not instance._source_switch_active
    assert any("分段时钟" in message for message in instance._status_bar.messages)


def test_video_switch_keeps_legacy_continuation_when_clock_is_unsafe(
    tmp_path: Path,
) -> None:
    first = tmp_path / "imou00024.mp4"
    second = tmp_path / "imou00025.mp4"
    _write_dahua(first, mtime_ms=1_000_000)
    _write_dahua(second, mtime_ms=1_002_600)
    instance = _SwitchHarness(first, 1_988_000)

    instance.open_video(str(second))

    assert instance.video_start_wall_ms == 10_500_000
    assert instance.playhead_ms == 500_000
    assert instance.align_method == "continuation"
