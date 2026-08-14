from __future__ import annotations

import numpy as np

from interaction_window import MainWindow as InteractiveWindow


class MainWindow(InteractiveWindow):
    """Adds activity navigation and resting-gravity diagnostics."""

    def __init__(self) -> None:
        self.activity_times_ms = np.empty(0, dtype=float)
        self.activity_values = np.empty(0, dtype=float)
        self.static_g = float("nan")
        super().__init__()

    def _display_motion_data(self) -> None:
        super()._display_motion_data()
        self._compute_activity()
        if np.isfinite(self.static_g):
            self.data_info.setText(
                self.data_info.text() + f" · 静止合加速度 {self.static_g:.3f} g"
            )

    def _compute_activity(self) -> None:
        if self.data is None:
            return
        channels = self.data.channels
        if not all(key in channels for key in ("ax", "ay", "az")):
            return
        times = np.asarray(self.data.times_ms, dtype=float)
        magnitude = np.sqrt(
            np.asarray(channels["ax"], dtype=float) ** 2
            + np.asarray(channels["ay"], dtype=float) ** 2
            + np.asarray(channels["az"], dtype=float) ** 2
        )
        if times.size == 0:
            return
        bins = np.floor(times / 1000.0).astype(np.int64)
        unique, starts = np.unique(bins, return_index=True)
        activity = np.empty(unique.size, dtype=float)
        means = np.empty(unique.size, dtype=float)
        ends = np.r_[starts[1:], times.size]
        for index, (start, end) in enumerate(zip(starts, ends)):
            segment = magnitude[start:end]
            activity[index] = float(np.std(segment))
            means[index] = float(np.mean(segment))
        self.activity_times_ms = unique.astype(float) * 1000.0 + 500.0
        self.activity_values = activity
        if activity.size:
            quiet = activity <= np.nanpercentile(activity, 20)
            if np.any(quiet):
                self.static_g = float(np.nanmean(means[quiet]) / 9.80665)

    def _jump_activity(self, direction: int) -> None:
        if self.activity_times_ms.size == 0:
            return
        threshold = np.nanpercentile(self.activity_values, 80)
        candidates = self.activity_times_ms[
            self.activity_values >= threshold
        ]
        if candidates.size == 0:
            return
        if direction > 0:
            future = candidates[candidates > self.playhead_ms + 1]
            target = future[0] if future.size else candidates[0]
        else:
            past = candidates[candidates < self.playhead_ms - 1]
            target = past[-1] if past.size else candidates[-1]
        self.set_playhead(float(target))

