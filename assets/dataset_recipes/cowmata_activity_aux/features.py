"""Timestamped VeDBA accumulation and past-only individual reference features."""
from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True)
class Config:
    acc_counts_per_g: float = 4096.0
    sample_rate_hz: int = 50
    gravity_window_samples: int = 100
    segment_gap_ms: int = 100
    minimum_hour_coverage: float = 0.65
    reference_minute_valid_seconds: float = 42.0
    minimum_reference_elapsed_hours: float = 2.0
    minimum_reference_valid_hours: float = 1.5
    minimum_reference_minutes: int = 90
    baseline_floor_g: float = 0.005
    evidence_ratio: float = 1.5
    strong_evidence_ratio: float = 2.0
    episode_cooldown_minutes: float = 120.0
    episode_reset_minutes: float = 120.0
    maximum_observation_age_minutes: float = 90.0
    history_retention_hours: int = 96
    max_packet_duration_ms: int = 7200000
    uid_retention_count: int = 2048

    def __post_init__(self):
        for key, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be positive and finite')
        for key in ['sample_rate_hz','gravity_window_samples','segment_gap_ms','minimum_reference_minutes',
                    'history_retention_hours','max_packet_duration_ms','uid_retention_count']:
            if not isinstance(getattr(self,key), int):
                raise ValueError(f'{key} must be an integer')
        if self.sample_rate_hz != 50 or self.gravity_window_samples != 100:
            raise ValueError('this version supports the validated 50 Hz / 100-sample preprocessing profile')
        if self.minimum_hour_coverage > 1 or self.reference_minute_valid_seconds > 60:
            raise ValueError('coverage must be <=1 and reference minute seconds <=60')
        if self.evidence_ratio >= self.strong_evidence_ratio:
            raise ValueError('strong_evidence_ratio must exceed evidence_ratio')
        if self.history_retention_hours < 74:
            raise ValueError('retain at least 74 hours for the 3-day same-clock reference')


def packet_minutes(packet, config):
    """Return [minute_end_ms, activity_sum, valid_count, observed_count] rows.

    Each complete uploaded packet is processed independently, matching v2.
    A >100 ms gap resets gravity; no resampling is performed across that gap.
    Unobserved time contributes no activity and no valid-time denominator.
    """
    t = packet.elapsed_ms
    first_minute = packet.start_ms // 60000
    n = packet.end_ms // 60000 - first_minute + 1
    observed = np.zeros(n)
    counts = np.zeros(n)
    sums = np.zeros(n)
    boundaries = np.r_[0, np.flatnonzero(np.diff(t) > config.segment_gap_ms) + 1, len(t)]
    for start, stop in zip(boundaries[:-1], boundaries[1:]):
        ts = t[start:stop]
        if len(ts) < 2:
            continue
        grid = np.arange(ts[0], ts[-1] + .1, 1000.0 / config.sample_rate_hz)
        values = np.column_stack([np.interp(grid, ts, packet.acceleration_counts[start:stop, k]) for k in range(3)])
        values /= config.acc_counts_per_g
        ix = ((grid + packet.create_ms) // 60000).astype(np.int64) - first_minute
        observed += np.bincount(ix, minlength=n)
        win = config.gravity_window_samples
        if len(grid) < win:
            continue
        cumulative = np.vstack([np.zeros((1, 3)), np.cumsum(values, axis=0)])
        gravity = (cumulative[win:] - cumulative[:-win]) / win
        activity = np.linalg.norm(values[win-1:] - gravity, axis=1)
        sums += np.bincount(ix[win-1:], weights=activity, minlength=n)
        counts += np.bincount(ix[win-1:], minlength=n)
    rows = [[int((first_minute+i+1)*60000), float(sums[i]), int(counts[i]), int(observed[i])]
            for i in range(n)]
    diagnostics = {'raw_frames': len(t), 'internal_gap_count': len(boundaries)-2,
                   'observed_seconds': float(observed.sum()/config.sample_rate_hz),
                   'valid_seconds': float(counts.sum()/config.sample_rate_hz),
                   'saturated_acceleration_frames': int(np.any((packet.acceleration_counts == -32768) |
                                                               (packet.acceleration_counts == 32767), axis=1).sum())}
    return rows, diagnostics


def compute_features(minutes, *, feature_end_ms, first_minute_end_ms, config):
    """Only accepted observations are passed in; no labels or predicted birth times."""
    rows = np.asarray(minutes, dtype=np.float64)
    end = rows[:, 0]
    sums, counts = rows[:, 1], rows[:, 2]
    sec = counts / config.sample_rate_hz
    avg = np.divide(sums, counts, out=np.full(len(rows), np.nan), where=counts > 0)

    def window_mean(stop, hours):
        mask = (end > stop-hours*3600000) & (end <= stop)
        count = counts[mask].sum()
        coverage = float(count/(config.sample_rate_hz*hours*3600))
        return (float(sums[mask].sum()/count) if count and coverage >= config.minimum_hour_coverage else None), coverage

    A1, c1 = window_mean(feature_end_ms, 1)
    A3, c3 = window_mean(feature_end_ms, 3)
    A6, c6 = window_mean(feature_end_ms, 6)
    hstop = feature_end_ms - 3600000
    hstart = feature_end_ms - 25*3600000
    reference = (end > hstart) & (end <= hstop) & (sec >= config.reference_minute_valid_seconds) & np.isfinite(avg)
    ref_seconds = float(sec[reference].sum())
    elapsed = max(0., min(24., (hstop-(first_minute_end_ms-60000))/3600000))
    features = {'activity_1h_g': A1, 'activity_3h_g': A3, 'activity_6h_g': A6,
        'coverage_1h': c1, 'coverage_3h': c3, 'coverage_6h': c6,
        'history_baseline_g': None, 'same_clock_baseline_g': None, 'baseline_g': None,
        'activity_ratio': None, 'activity_excess_ratio': None, 'activity_ratio_3h': None,
        'reference_valid_hours': ref_seconds/3600, 'reference_elapsed_hours': elapsed,
        'reference_valid_minutes': int(reference.sum()), 'reference_days': 0,
        'same_clock_weight': 0., 'reference_quality': 'INSUFFICIENT_HISTORY'}
    if (ref_seconds < config.minimum_reference_valid_hours*3600 or
            elapsed < config.minimum_reference_elapsed_hours or reference.sum() < config.minimum_reference_minutes):
        return features
    history = float(np.average(avg[reference], weights=sec[reference]))
    past = [window_mean(feature_end_ms-day*86400000, 1)[0] for day in [1, 2, 3]]
    past = [x for x in past if x is not None]
    days = len(past)
    clock = float(np.median(past)) if days else None
    weight = days / (days+2.)
    baseline = weight*clock + (1-weight)*history if days else history
    ratio = A1 / baseline if A1 is not None and baseline >= config.baseline_floor_g else None
    features.update(history_baseline_g=history, same_clock_baseline_g=clock, baseline_g=baseline,
                    activity_ratio=ratio, activity_excess_ratio=ratio-1 if ratio is not None else None,
                    activity_ratio_3h=A3/baseline if A3 is not None and baseline >= config.baseline_floor_g else None,
                    reference_days=days, same_clock_weight=weight,
                    reference_quality='SAME_CLOCK_BLEND' if days else 'PROVISIONAL_SAME_DAY')
    return features
