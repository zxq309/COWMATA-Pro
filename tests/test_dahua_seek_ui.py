from __future__ import annotations

from cowmata_tailring.app.mixins.production_safety import (
    ProductionSafetyMixin,
)


class _Timer:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class _Timeline:
    _dragging = True


class _Media:
    def __init__(self, defer: bool) -> None:
        self.defer = defer

    def defers_timeline_seek_while_dragging(self) -> bool:
        return self.defer


def _mixin(*, defer: bool) -> ProductionSafetyMixin:
    instance = object.__new__(ProductionSafetyMixin)
    instance.media = _Media(defer)
    instance.video_timeline = _Timeline()
    instance._video_seek_timer = _Timer()
    instance._pending_video_seek_ms = None
    return instance


def test_dahua_drag_updates_target_without_starting_decoder_seek() -> None:
    instance = _mixin(defer=True)

    instance._queue_video_timeline_seek(123_456)

    assert instance._pending_video_seek_ms == 123_456
    assert instance._video_seek_timer.started == 0
    assert instance._video_seek_timer.stopped == 1


def test_dahua_mouse_release_starts_one_decoder_seek() -> None:
    instance = _mixin(defer=True)
    instance._queue_video_timeline_seek(123_456)

    instance.video_timeline._dragging = False
    instance._queue_video_timeline_seek(234_567)

    assert instance._pending_video_seek_ms == 234_567
    assert instance._video_seek_timer.started == 1


def test_regular_video_keeps_drag_preview_seek() -> None:
    instance = _mixin(defer=False)

    instance._queue_video_timeline_seek(123_456)

    assert instance._video_seek_timer.started == 1
    assert instance._video_seek_timer.stopped == 0
