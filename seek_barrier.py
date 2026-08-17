from __future__ import annotations

import time


class PlaybackSeekBarrier:
    """Reject timestamps from the decoder state that preceded a seek."""

    POSITION_TOLERANCE_MS = 100.0
    CONFIRM_TIMEOUT_S = 8.0

    def __init__(self) -> None:
        self.target_ms: float | None = None
        self.phase = "idle"
        self.deadline = 0.0
        self.confirmation_serial = 0
        self.confirmed_target_ms: float | None = None
        self.confirmed_video_ms: float | None = None

    def reset(self) -> None:
        self.target_ms = None
        self.phase = "idle"
        self.deadline = 0.0
        self.confirmed_target_ms = None
        self.confirmed_video_ms = None

    def request(
        self,
        target_ms: float,
        *,
        playing: bool,
        now: float | None = None,
    ) -> None:
        moment = time.monotonic() if now is None else float(now)
        self.target_ms = max(0.0, float(target_ms))
        self.confirmed_target_ms = None
        self.confirmed_video_ms = None
        self.phase = "verifying" if playing else "waiting"
        self.deadline = moment + self.CONFIRM_TIMEOUT_S

    def playback_started(self, now: float | None = None) -> float | None:
        if self.target_ms is None:
            return None
        moment = time.monotonic() if now is None else float(now)
        self.phase = "verifying"
        self.deadline = moment + self.CONFIRM_TIMEOUT_S
        return self.target_ms

    def target_is_ready(self, target_ms: float) -> bool:
        if self.target_ms is None:
            return bool(
                self.confirmed_target_ms is not None
                and abs(float(target_ms) - self.confirmed_target_ms) <= 1.0
            )
        if abs(float(target_ms) - self.target_ms) > 1.0:
            return False
        return self.phase == "paused_ready"

    def playback_confirmation_pending(self) -> bool:
        return self.target_ms is not None and self.phase == "verifying"

    def expire(self, now: float | None = None) -> bool:
        """Release a stale confirmation even when VLC's time is unchanged."""

        if self.target_ms is None:
            return False
        moment = time.monotonic() if now is None else float(now)
        if moment < self.deadline:
            return False
        self.reset()
        return True

    def _confirm(self, video_ms: float) -> None:
        target = self.target_ms
        if target is None:
            return
        self.confirmed_target_ms = target
        self.confirmed_video_ms = float(video_ms)
        self.confirmation_serial += 1
        self.target_ms = None
        self.phase = "idle"
        self.deadline = 0.0

    def observe(
        self,
        video_ms: float,
        *,
        playing: bool,
        rate: float,
        now: float | None = None,
    ) -> bool:
        if self.target_ms is None:
            return True
        moment = time.monotonic() if now is None else float(now)
        if self.expire(moment):
            return True

        value = float(video_ms)
        target = self.target_ms
        tolerance = self.POSITION_TOLERANCE_MS
        if not playing:
            if abs(value - target) <= tolerance:
                self._confirm(value)
                return True
            return False

        self.phase = "verifying"
        # The first genuine timestamp after a 10x seek may already be several
        # media seconds past the target.  Old decoder timestamps are normally
        # much farther away and must not escape to the UI.
        upper_window = max(5_000.0, max(0.05, float(rate)) * 1_000.0)
        if value < target - tolerance or value > target + upper_window:
            return False
        # Reaching the requested position is itself a valid confirmation.
        # Requiring a second, later VLC timestamp made the UI wait forever
        # on streams whose picture advances while get_time() stays frozen.
        self._confirm(value)
        return True


__all__ = ["PlaybackSeekBarrier"]
