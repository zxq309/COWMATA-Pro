from __future__ import annotations


class TimerShutdownMixin:
    """Stop UI clocks and queued callbacks before the media engine closes."""

    def __init__(self) -> None:
        self._desktop_closing = False
        super().__init__()

    def _finish_media_priming(self) -> None:
        if self._desktop_closing:
            return
        super()._finish_media_priming()

    def _play_after_priming(self) -> None:
        if self._desktop_closing:
            return
        super()._play_after_priming()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._desktop_closing = True
        self._prime_generation += 1
        self._queued_play_after_prime = False
        self._prime_pending_seek_ms = None
        self._prime_pending_seek_is_initial = False
        self._prime_duration_ready = False
        self._prime_decoder_started = False
        self._prime_frame_ready = False
        self._prime_pause_requested = False
        self._ui_pending_seek_video_ms = None
        self._smooth_playing = False
        for name in (
            "_seek_watchdog",
            "_video_seek_timer",
            "_smooth_timer",
        ):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        super().closeEvent(event)
        # PlaybackMixin's short single-shot pause callback checks this
        # attribute before calling into libVLC.
        self.media = None
