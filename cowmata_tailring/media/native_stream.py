"""Bounded native PS playback with an unscaled, real packet clock."""
from .dahua_stream import DahuaPlaybackStream


class NativePlaybackStream(DahuaPlaybackStream):
    def __init__(self, index, target_ms):
        super().__init__(index, target_ms)
        # Do not time-compress preroll for HK/Shenmo. Decoders may drop or hold
        # reference frames if a whole GOP is squeezed into 150 ms. The UI waits
        # for the actual packet clock; paused inspection uses indexed FFmpeg.
        self.pre_roll_ms = max(0, self.target_ms-self.anchor_ms)
        self.target_hold_ms = 0

    def _output_time_ms(self, logical_time_ms):
        return max(0, logical_time_ms-self.anchor_ms)

    def public_time_ms(self, raw_time_ms):
        return max(0, min(self.index.duration_ms, round(self.anchor_ms+max(0,raw_time_ms))))
