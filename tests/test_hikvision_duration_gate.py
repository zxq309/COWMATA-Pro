from __future__ import annotations

from cowmata_tailring.media.safe_engine import (
    SafeMediaEngine,
    _timeline_transport_blocked,
)


class _Lib:
    def __init__(self) -> None:
        self.pause_values: list[int] = []

    def libvlc_media_player_set_pause(self, _player, paused: int) -> None:
        self.pause_values.append(paused)


class _Barrier:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1


class _Signal:
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def emit(self, message: str, timeout_ms: int) -> None:
        self.events.append((message, timeout_ms))


class _FailureHarness:
    _reject_unvalidated_timeline = (
        SafeMediaEngine._reject_unvalidated_timeline
    )

    def __init__(self) -> None:
        self._timeline_probe_pending = True
        self._timeline_validation_failed = False
        self._timeline_validation_error = ""
        self._pending_seek_ms = 123_456
        self._seek_barrier = _Barrier()
        self._pause_requested = False
        self._player = object()
        self._lib = _Lib()
        self._last_duration = 30_821_470
        self.timeline_analysis_message = _Signal()


def test_hikvision_raw_duration_is_blocked_until_index_exists() -> None:
    assert _timeline_transport_blocked(True, None)
    assert not _timeline_transport_blocked(True, object())


def test_regular_video_does_not_require_timeline_index() -> None:
    assert not _timeline_transport_blocked(False, None)


def test_failed_validation_pauses_and_discards_untrusted_transport() -> None:
    engine = _FailureHarness()

    engine._reject_unvalidated_timeline("ffprobe missing")

    assert not engine._timeline_probe_pending
    assert engine._timeline_validation_failed
    assert engine._timeline_validation_error == "ffprobe missing"
    assert engine._pending_seek_ms is None
    assert engine._seek_barrier.reset_calls == 1
    assert engine._pause_requested
    assert engine._lib.pause_values == [1]
    assert engine._last_duration == -1
    assert engine.timeline_analysis_message.events == [
        (
            "海康视频时间轴校验失败，已禁用时长和跳转；"
            "请检查 FFprobe 或视频文件",
            0,
        )
    ]
