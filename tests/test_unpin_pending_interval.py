"""Unpinning must not discard or block an unfinished annotation interval."""

from __future__ import annotations

from cowmata_tailring.media.mixins.dual_anchor_precision import (
    DualAnchorPrecisionV2Mixin,
)


class _Media:
    def __init__(self) -> None:
        self.paused = False

    def is_playing(self) -> bool:
        return False

    def pause(self, paused: bool) -> None:
        self.paused = bool(paused)

    def get_time_ms(self) -> float:
        return 456.0


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, message: str, timeout: int = 0) -> None:  # noqa: N802
        self.messages.append((message, timeout))


class _Harness(DualAnchorPrecisionV2Mixin):
    def __init__(self) -> None:
        self._timelines_linked = True
        self.pending_intervals = {4: 123.0}
        self.playhead_ms = 234.0
        self.video_path = "video.mp4"
        self.media = _Media()
        self._status_bar = _StatusBar()
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._independent_video_selection_active = False
        self.link_updates: list[bool] = []
        self.pending_pair_shown = False
        self.autosaved = False

    def statusBar(self) -> _StatusBar:  # noqa: N802
        return self._status_bar

    def _set_timelines_linked(self, linked: bool) -> None:
        self._timelines_linked = bool(linked)
        self.link_updates.append(bool(linked))

    def _show_pending_pair(self) -> None:
        self.pending_pair_shown = True

    def _autosave(self) -> None:
        self.autosaved = True


def test_unpin_preserves_pending_interval_and_enters_calibration() -> None:
    window = _Harness()

    window.pin_alignment()

    assert window._timelines_linked is False
    assert window.pending_intervals == {4: 123.0}
    assert window._pending_data_anchor_ms == 234.0
    assert window._pending_video_anchor_ms == 456.0
    assert window._independent_video_selection_active is True
    assert window.link_updates == [False]
    assert window.pending_pair_shown is True
    assert window.autosaved is True
    assert window._status_bar.messages[-1] == (
        "已解除钉住；未结束区间已保留，重新钉住后可继续或按 Esc 取消",
        5000,
    )
