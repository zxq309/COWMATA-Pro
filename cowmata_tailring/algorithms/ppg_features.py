"""Optical feature schema; requires a PPG-trained model and configured timing."""

import numpy as np

from cowmata_tailring.workspace.sensor_records import parse_ppg_object

VERSION = "ppg-second-features-1"
CHANNELS = ("ppg_primary", "ppg_ir", "ppg_red", "ax", "ay", "az")
STATS = ("mean", "std", "range", "difference_rms", "relative_variation")


def extract(document, path):
    ppg = parse_ppg_object(document, path)
    count = int(np.ceil((ppg.duration_ms + 1000 / ppg.sample_rate_hz) / 1000))
    if count <= 1:
        raise ValueError("PPG 记录过短")
    names = [channel + "_" + stat for channel in CHANNELS for stat in STATS]
    x = np.full((count, len(names)), np.nan)
    valid = np.zeros(count, dtype=bool)
    for second in range(count):
        lo, hi = max(0, second - 2) * 1000, min(count, second + 3) * 1000
        mask = (ppg.times_ms >= lo) & (ppg.times_ms < hi)
        for channel_index, channel in enumerate(CHANNELS):
            if channel not in ppg.channels:
                continue
            values = np.asarray(ppg.channels[channel][2][mask], dtype=float)
            values = values[np.isfinite(values)]
            if len(values) < 4:
                continue
            avg, std = float(np.mean(values)), float(np.std(values))
            delta = float(np.sqrt(np.mean(np.diff(values) ** 2)))
            x[second, channel_index * 5 : (channel_index + 1) * 5] = [
                avg,
                std,
                float(np.ptp(values)),
                delta,
                std / max(abs(avg), 1e-6),
            ]
            if channel.startswith("ppg_") and std > 0:
                valid[second] = True
    acc_columns = [
        i * 5 + 3 for i, c in enumerate(CHANNELS) if c in ("ax", "ay", "az") and c in ppg.channels
    ]
    dynamic = np.nanmean(x[:, acc_columns], axis=1) if acc_columns else np.full(count, np.nan)
    return dict(
        feature_version=VERSION,
        modality="ppg",
        X=x,
        names=names,
        seconds=np.arange(count),
        valid=valid,
        valid_context=valid.copy(),
        dynamic=dynamic,
        segments=[(0, ppg.duration_ms)],
        observed_seconds=int(valid.sum()),
        duration_ms=ppg.duration_ms,
        epoch_offset_ms=ppg.epoch_at(0),
        temperature_c=np.array([]),
        temperature_ms=np.array([]),
        gap_count=0,
        sample_count=ppg.sample_count,
        sample_rate_hz=ppg.sample_rate_hz,
        warnings=["PPG 专用特征，不能使用九轴模型；光学振幅不直接换算血氧或心率"],
    )
