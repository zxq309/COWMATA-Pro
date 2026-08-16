"""2026-08-16 offline 104-feature contract used by the full GBDT model.

This is the inference-only runtime copy of ``cattle_imu.features``.  Keep its
numeric operations aligned with the training source: centred filters/windows,
per-session mounting self-calibration, and strict segment isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import butter, sosfilt, sosfiltfilt


SAMPLE_RATE_HZ = 50.0
GRAVITY_CUTOFF_HZ = 0.3
SCALE_SECONDS = (1.0, 5.0, 15.0, 30.0)
TRANSITION_SECONDS = (2.0, 5.0, 10.0)
TILT_PLATEAU_DEG = (5.0, 10.0, 20.0, 40.0)
LONG_BASELINE_SECONDS = 1800.0


def window_stats(
    values: np.ndarray,
    lo_offset: int,
    hi_offset: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Mean/std over clipped ``[i + lo_offset, i + hi_offset)`` windows."""

    data = np.asarray(values, dtype=np.float64).ravel()
    size = data.size
    if size == 0:
        return np.empty(0), np.empty(0)
    cumulative = np.concatenate(([0.0], np.cumsum(data)))
    cumulative_square = np.concatenate(([0.0], np.cumsum(data * data)))
    indices = np.arange(size, dtype=np.int64)
    low = np.clip(indices + int(lo_offset), 0, size - 1)
    high = np.clip(indices + int(hi_offset), 1, size)
    high = np.maximum(high, low + 1)
    low = np.minimum(low, high - 1)
    count = (high - low).astype(np.float64)
    total = cumulative[high] - cumulative[low]
    total_square = cumulative_square[high] - cumulative_square[low]
    mean = total / count
    variance = np.maximum(total_square / count - mean * mean, 0.0)
    return mean, np.sqrt(variance)


def window_max(values: np.ndarray, lo_offset: int, hi_offset: int) -> np.ndarray:
    """Rolling maximum with the exact centred/trailing alignment from training."""

    series = pd.Series(np.asarray(values, dtype=np.float64).ravel())
    span = int(hi_offset) - int(lo_offset)
    if span <= 0:
        raise ValueError("hi_offset must exceed lo_offset")
    rolled = series.rolling(window=span, min_periods=1).max().to_numpy()
    shift = int(hi_offset) - 1
    output = np.empty_like(rolled)
    if shift > 0:
        output[:-shift or None] = rolled[shift:]
        output[len(output) - shift :] = rolled[-1]
    elif shift < 0:
        output[-shift:] = rolled[:shift]
        output[:-shift] = rolled[0]
    else:
        output = rolled
    return output


def _bandpass(
    values: np.ndarray,
    low: float,
    high: float,
    *,
    causal: bool,
) -> np.ndarray:
    size = values.shape[0]
    if size < 64:
        return np.asarray(values, dtype=np.float64)
    nyquist = SAMPLE_RATE_HZ / 2.0
    low_normalized = max(low / nyquist, 1e-4)
    high_normalized = min(high / nyquist, 0.99)
    if high_normalized <= low_normalized:
        return np.asarray(values, dtype=np.float64)
    sections = butter(
        4,
        [low_normalized, high_normalized],
        btype="bandpass",
        output="sos",
    )
    data = np.asarray(values, dtype=np.float64)
    if causal:
        return sosfilt(sections, data, axis=0)
    padding = 3 * (sections.shape[0] * 2)
    return (
        sosfiltfilt(sections, data, axis=0)
        if size > padding + 1
        else sosfilt(sections, data, axis=0)
    )


def gravity_split(
    acceleration: np.ndarray,
    *,
    causal: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Split acceleration into low-pass gravity and dynamic motion."""

    data = np.asarray(acceleration, dtype=np.float64)
    size = data.shape[0]
    if size < 64:
        static = np.repeat(data.mean(axis=0, keepdims=True), size, axis=0)
        return static, data - static
    sections = butter(
        4,
        GRAVITY_CUTOFF_HZ / (SAMPLE_RATE_HZ / 2.0),
        btype="lowpass",
        output="sos",
    )
    if causal:
        static = sosfilt(sections, data, axis=0)
    else:
        padding = 3 * (sections.shape[0] * 2)
        static = (
            sosfiltfilt(sections, data, axis=0)
            if size > padding + 1
            else sosfilt(sections, data, axis=0)
        )
    return static, data - static


@dataclass(frozen=True)
class SessionReference:
    gravity_unit: np.ndarray
    rotation: np.ndarray
    quiet_fraction: float

    def to_dict(self) -> dict[str, object]:
        return {
            "gravity_unit": [float(value) for value in self.gravity_unit],
            "rotation": [
                [float(value) for value in row] for row in self.rotation
            ],
            "quiet_fraction": float(self.quiet_fraction),
        }


def session_reference(
    static_acceleration: np.ndarray,
    vedba: np.ndarray,
    *,
    quiet_quantile: float = 0.3,
) -> SessionReference:
    """Estimate one ring's mounting orientation from its quietest samples."""

    static = np.asarray(static_acceleration, dtype=np.float64)
    motion = np.asarray(vedba, dtype=np.float64).ravel()
    if static.shape[0] == 0:
        unit = np.asarray([0.0, 0.0, 1.0])
        return SessionReference(unit, np.eye(3), 0.0)
    threshold = np.quantile(motion, quiet_quantile) if motion.size else np.inf
    quiet = motion <= threshold
    if quiet.sum() < 10:
        quiet = np.ones(static.shape[0], dtype=bool)
    reference = np.median(static[quiet], axis=0)
    norm = float(np.linalg.norm(reference))
    unit = (
        reference / norm
        if norm > 1e-9
        else np.asarray([0.0, 0.0, 1.0])
    )
    return SessionReference(
        unit,
        _rotation_to_z(unit),
        float(quiet.mean()),
    )


def _rotation_to_z(unit: np.ndarray) -> np.ndarray:
    source = np.asarray(unit, dtype=np.float64)
    target = np.asarray([0.0, 0.0, 1.0])
    cross = np.cross(source, target)
    cosine = float(np.dot(source, target))
    sine = float(np.linalg.norm(cross))
    if sine < 1e-9:
        return np.eye(3) if cosine > 0 else np.diag([1.0, -1.0, -1.0])
    matrix = np.asarray(
        [
            [0.0, -cross[2], cross[1]],
            [cross[2], 0.0, -cross[0]],
            [-cross[1], cross[0], 0.0],
        ]
    )
    return np.eye(3) + matrix + matrix @ matrix * ((1.0 - cosine) / sine**2)


def derived_series(
    segment_array: np.ndarray,
    *,
    causal: bool,
    reference: SessionReference | None = None,
) -> dict[str, np.ndarray]:
    array = np.asarray(segment_array, dtype=np.float64)
    acceleration = array[:, 0:3]
    gyroscope = array[:, 3:6]
    quality = array[:, 12] if array.shape[1] > 12 else np.zeros(len(array))
    static, dynamic = gravity_split(acceleration, causal=causal)
    static_norm = np.linalg.norm(static, axis=1)
    unit = static / np.maximum(static_norm, 1e-9)[:, None]
    vedba = np.linalg.norm(dynamic, axis=1)
    odba = np.abs(dynamic).sum(axis=1)
    reference = reference or session_reference(static, vedba)
    body_unit = unit @ reference.rotation.T
    tilt = np.degrees(
        np.arccos(np.clip(body_unit[:, 2], -1.0, 1.0))
    )
    pitch = np.degrees(
        np.arctan2(static[:, 0], np.hypot(static[:, 1], static[:, 2]))
    )
    roll = np.degrees(
        np.arctan2(static[:, 1], np.hypot(static[:, 0], static[:, 2]))
    )
    low = _bandpass(gyroscope, 0.5, 2.0, causal=causal)
    middle = _bandpass(gyroscope, 2.0, 5.0, causal=causal)
    high = _bandpass(gyroscope, 5.0, 15.0, causal=causal)
    return {
        "tilt_deg": tilt,
        "body_ux": body_unit[:, 0],
        "body_uy": body_unit[:, 1],
        "body_uz": body_unit[:, 2],
        "pitch_deg": pitch,
        "roll_deg": roll,
        "static_norm": static_norm,
        "vedba": vedba,
        "odba": odba,
        "gyro_norm": np.linalg.norm(gyroscope, axis=1),
        "gyro_x": gyroscope[:, 0],
        "gyro_y": gyroscope[:, 1],
        "gyro_z": gyroscope[:, 2],
        "dyn_x": dynamic[:, 0],
        "dyn_y": dynamic[:, 1],
        "dyn_z": dynamic[:, 2],
        "band_low_energy": np.square(low).sum(axis=1),
        "band_mid_energy": np.square(middle).sum(axis=1),
        "band_high_energy": np.square(high).sum(axis=1),
        "quality_flag": quality,
    }


def _offsets(seconds: float, *, causal: bool) -> tuple[int, int]:
    span = max(1, int(round(seconds * SAMPLE_RATE_HZ)))
    if causal:
        return -span + 1, 1
    half = span // 2
    return -half, half + 1


def segment_features(
    segment_array: np.ndarray,
    local_centers: np.ndarray,
    *,
    causal: bool,
    reference: SessionReference | None = None,
) -> pd.DataFrame:
    """Create the model's 104 features for centers inside one segment."""

    series = derived_series(
        segment_array,
        causal=causal,
        reference=reference,
    )
    centers = np.asarray(local_centers, dtype=np.int64)
    size = segment_array.shape[0]
    if centers.size == 0:
        return pd.DataFrame()
    if centers.min() < 0 or centers.max() >= size:
        raise IndexError("local centre outside segment")
    columns: dict[str, np.ndarray] = {}

    def add(name: str, values: np.ndarray) -> None:
        columns[name] = np.asarray(values, dtype=np.float32)[centers]

    tilt = series["tilt_deg"]
    for seconds in SCALE_SECONDS:
        low, high = _offsets(seconds, causal=causal)
        tag = f"{seconds:g}s".replace(".", "p")
        mean, standard_deviation = window_stats(tilt, low, high)
        add(f"tilt_mean_{tag}", mean)
        add(f"tilt_std_{tag}", standard_deviation)
        add(f"tilt_max_{tag}", window_max(tilt, low, high))
        for degrees in TILT_PLATEAU_DEG:
            fraction, _ = window_stats(
                (tilt > degrees).astype(np.float64), low, high
            )
            add(f"tilt_frac_gt{int(degrees)}_{tag}", fraction)
        for key in ("vedba", "odba", "gyro_norm"):
            mean, standard_deviation = window_stats(series[key], low, high)
            add(f"{key}_mean_{tag}", mean)
            add(f"{key}_std_{tag}", standard_deviation)
        add(f"vedba_max_{tag}", window_max(series["vedba"], low, high))
        for key in (
            "band_low_energy",
            "band_mid_energy",
            "band_high_energy",
        ):
            mean, _ = window_stats(series[key], low, high)
            add(f"{key}_mean_{tag}", mean)
        if seconds <= 5.0:
            for key in (
                "gyro_x",
                "gyro_y",
                "gyro_z",
                "dyn_x",
                "dyn_y",
                "dyn_z",
            ):
                _, standard_deviation = window_stats(series[key], low, high)
                add(f"{key}_std_{tag}", standard_deviation)
            for key in (
                "body_ux",
                "body_uy",
                "body_uz",
                "pitch_deg",
                "roll_deg",
            ):
                mean, _ = window_stats(series[key], low, high)
                add(f"{key}_mean_{tag}", mean)

    for seconds in TRANSITION_SECONDS:
        span = max(1, int(round(seconds * SAMPLE_RATE_HZ)))
        tag = f"{seconds:g}s".replace(".", "p")
        before = (-2 * span, -span)
        after = ((-span, 1) if causal else (span, 2 * span))
        before_vector = np.stack(
            [
                window_stats(series[key], *before)[0]
                for key in ("body_ux", "body_uy", "body_uz")
            ],
            axis=1,
        )
        after_vector = np.stack(
            [
                window_stats(series[key], *after)[0]
                for key in ("body_ux", "body_uy", "body_uz")
            ],
            axis=1,
        )
        before_vector /= np.maximum(
            np.linalg.norm(before_vector, axis=1, keepdims=True), 1e-9
        )
        after_vector /= np.maximum(
            np.linalg.norm(after_vector, axis=1, keepdims=True), 1e-9
        )
        cosine = np.clip((before_vector * after_vector).sum(axis=1), -1.0, 1.0)
        add(f"transition_angle_{tag}", np.degrees(np.arccos(cosine)))
        add(
            f"transition_tilt_delta_{tag}",
            window_stats(tilt, *after)[0] - window_stats(tilt, *before)[0],
        )
        add(
            f"transition_vedba_ratio_{tag}",
            (window_stats(series["vedba"], *after)[0] + 1e-6)
            / (window_stats(series["vedba"], *before)[0] + 1e-6),
        )

    baseline = _long_baseline(tilt)
    add("tilt_rel_baseline", tilt - baseline)
    add("tilt_baseline", baseline)
    quality_mean, _ = window_stats(
        series["quality_flag"], *_offsets(5.0, causal=causal)
    )
    add("quality_mean_5s", quality_mean)
    columns["segment_position"] = (
        centers / max(size - 1, 1)
    ).astype(np.float32)
    columns["segment_length"] = np.full(
        centers.size, float(size), dtype=np.float32
    )
    return pd.DataFrame(columns)


def _long_baseline(
    tilt: np.ndarray,
    seconds: float = LONG_BASELINE_SECONDS,
) -> np.ndarray:
    size = tilt.size
    step = int(SAMPLE_RATE_HZ)
    coarse = pd.Series(tilt[::step])
    median = coarse.rolling(
        window=max(1, int(seconds)), min_periods=1
    ).median().to_numpy()
    if median.size * step >= size:
        return np.repeat(median, step)[:size]
    return np.interp(np.arange(size), np.arange(median.size) * step, median)
