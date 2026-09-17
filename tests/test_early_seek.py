from __future__ import annotations

from cowmata_tailring.media.mixins.early_seek import EarlySeekMixin


class _Media:
    def __init__(self) -> None:
        self.seek_targets: list[float] = []

    def set_time_ms(self, target: float) -> bool:
        self.seek_targets.append(float(target))
        return True


class _Base:
    def __init__(self) -> None:
        self.media = _Media()
        self._ui_pending_seek_video_ms: float | None = 12_345.0
        self.duration_events: list[int] = []

    def _on_media_duration(self, duration_ms: int) -> None:
        self.duration_events.append(duration_ms)


class _Window(EarlySeekMixin, _Base):
    pass


def test_regular_seek_does_not_restart_on_duration_signal() -> None:
    window = _Window()

    window._on_media_duration(60_000)

    assert window.duration_events == [60_000]
    assert window.media.seek_targets == []


def test_early_seek_consumes_only_one_duration_signal() -> None:
    window = _Window()
    window._early_seek_waiting_for_duration = True

    window._on_media_duration(60_000)
    window._on_media_duration(60_000)

    assert window.duration_events == [60_000, 60_000]
    assert window.media.seek_targets == [12_345.0]
    assert not window._early_seek_waiting_for_duration
