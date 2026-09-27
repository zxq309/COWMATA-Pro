"""Pulse-rate estimation from one tail-base PPG recording (IR primary, red confirm).

Only numpy/scipy are used so the same code runs in the portable runtime. The
estimate is a pulse rate from the optical waveform; it is reported with its own
signal-quality grade and is never filled in when the waveform is unusable.
"""
from __future__ import annotations

import base64
import json
import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import signal

VERSION = "ppg-hr-1.0.0"
RATES = (50, 100, 200, 400, 800, 1000, 1600, 3200)
GRADES = ("HIGH", "MEDIUM", "LOW", "REJECT")


@dataclass(frozen=True)
class EstimatorConfig:
    min_bpm: float = 40.0
    max_bpm: float = 140.0
    band_low_hz: float = 0.6
    band_high_hz: float = 5.0
    window_seconds: float = 16.0
    step_seconds: float = 4.0
    block_seconds: float = 2.0
    motion_block_ratio: float = 3.0
    minimum_clean_fraction: float = 0.35
    minimum_windows: int = 3
    nfft: int = 8192
    agreement_bpm: float = 6.0
    minimum_dc: float = 2000.0
    saturation_counts: int = 262000


def decode_record(document):
    """Return (fs, duration_s, {'ir': array, 'red': array}, meta) from a PPG JSON."""
    config = document.get("configs") or {}
    if isinstance(config, str):
        config = json.loads(config)
    sr, average = config.get("pulse_led_sr"), config.get("pulse_led_avr")
    fs = float(document.get("sample_rate_hz") or 0)
    if sr is not None and average is not None and 0 <= int(sr) < len(RATES) and 0 <= int(average) <= 10:
        fs = RATES[int(sr)] / (1 << int(average))
    duration = float(config.get("pulse_sample_time") or 0)
    channels = {}
    for field, key in (("ir_data", "ir"), ("data", "red"), ("red_data", "red")):
        value = document.get(field)
        if not value or key in channels:
            continue
        raw = base64.b64decode("".join(str(value).split()), validate=True)
        expected = round(fs * duration) if fs > 0 and duration > 0 else 0
        widths = [w for w in (4, 2) if len(raw) % w == 0]
        if not widths:
            raise ValueError("PPG 通道字节数不是整样本")
        width = min(widths, key=lambda w: abs(len(raw) // w - expected)) if expected else widths[0]
        channels[key] = np.frombuffer(raw, dtype="<u4" if width == 4 else "<u2").astype(float)
    primary = "ir"
    if "ir" not in channels:
        if "red" not in channels:
            raise ValueError("PPG 记录缺少光学通道")
        # Single-LED firmware stores the only pulse channel in ``data``.
        channels = {"ir": channels["red"]}
        primary = "red_only"
    if fs <= 0 and duration > 0:
        fs = len(channels["ir"]) / duration
    if fs <= 0:
        raise ValueError("PPG 缺少采样率或采样时长")
    meta = dict(led_mode=config.get("pulse_led_mode"), work_mode=config.get("work_mode"),
                led_width=config.get("pulse_led_width"), led_field=document.get("led"),
                primary_channel=primary)
    return fs, len(channels["ir"]) / fs, channels, meta


def _bandpass(x, fs, c):
    sos = signal.butter(3, [c.band_low_hz, min(c.band_high_hz, 0.45 * fs)], "bandpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x - np.median(x))


def _clean_mask(y, fs, c):
    block = int(c.block_seconds * fs)
    n = len(y) // block
    if n < 4:
        return np.zeros(len(y), bool), 0.0
    amp = np.array([np.subtract(*np.percentile(y[i * block:(i + 1) * block], [95, 5])) for i in range(n)])
    ref = np.median(amp)
    if not np.isfinite(ref) or ref <= 0:
        return np.zeros(len(y), bool), 0.0
    good = (amp <= c.motion_block_ratio * ref) & (amp >= ref / c.motion_block_ratio)
    # A block next to a motion burst carries filter ringing; drop its neighbours too.
    bad = ~good
    spread = bad | np.r_[False, bad[:-1]] | np.r_[bad[1:], False]
    mask = np.zeros(len(y), bool)
    for i in np.flatnonzero(~spread):
        mask[i * block:(i + 1) * block] = True
    return mask, float(mask.mean())


def _harmonic_spectrum(window, fs, c):
    """Harmonic-sum spectral peak: the fundamental needs support at 2f and 3f."""
    w = window * np.hanning(len(window))
    power = np.abs(np.fft.rfft(w, c.nfft)) ** 2
    freq = np.fft.rfftfreq(c.nfft, 1 / fs)
    band = (freq >= c.band_low_hz) & (freq <= c.band_high_hz)
    total = float(power[band].sum()) or 1.0
    grid = freq[(freq >= c.min_bpm / 60) & (freq <= c.max_bpm / 60)]
    p1 = np.interp(grid, freq, power)
    p2 = np.interp(2 * grid, freq, power)
    p3 = np.interp(3 * grid, freq, power)
    score = p1 + 0.6 * p2 + 0.3 * p3
    k = int(np.argmax(score))
    f0 = grid[k]
    if 0 < k < len(grid) - 1:
        a, b, d = score[k - 1], score[k], score[k + 1]
        den = a - 2 * b + d
        if den:
            f0 = f0 + 0.5 * (a - d) / den * (grid[1] - grid[0])

    def near(f):
        return (freq >= f - 4 / 60) & (freq <= f + 4 / 60)

    sqi = float((power[near(f0)].sum() + power[near(2 * f0)].sum()) / total)
    return float(f0 * 60), sqi


def _window_estimates(y, mask, fs, c):
    win, step = int(c.window_seconds * fs), int(c.step_seconds * fs)
    rows = []
    for start in range(0, len(y) - win + 1, step):
        if mask[start:start + win].mean() < 0.9:
            continue
        seg = y[start:start + win]
        seg = (seg - seg.mean()) / (seg.std() or 1.0)
        bpm, sqi = _harmonic_spectrum(seg, fs, c)
        rows.append((start / fs, bpm, sqi))
    return rows


def _acf_rate(y, mask, fs, c):
    """Median first dominant autocorrelation lag over clean windows (time-domain check)."""
    win, step = int(c.window_seconds * fs), int(c.step_seconds * fs)
    lo, hi = int(fs * 60 / c.max_bpm), int(fs * 60 / c.min_bpm)
    rates, strengths = [], []
    for start in range(0, len(y) - win + 1, step):
        if mask[start:start + win].mean() < 0.9:
            continue
        seg = y[start:start + win] - y[start:start + win].mean()
        full = np.correlate(seg, seg, "full")[len(seg) - 1:]
        if full[0] <= 0:
            continue
        r = full / full[0]
        part = r[lo:hi + 1]
        peaks, props = signal.find_peaks(part, height=0.2)
        if not len(peaks):
            continue
        best = peaks[int(np.argmax(props["peak_heights"]))]
        # Prefer the shortest lag whose correlation is close to the best one (avoids 1/2 rate).
        close = peaks[props["peak_heights"] >= 0.85 * props["peak_heights"].max()]
        lag = int(close[0]) if len(close) else int(best)
        k = lag + lo
        if 0 < k < len(r) - 1:
            a, b, d = r[k - 1], r[k], r[k + 1]
            den = a - 2 * b + d
            k = k + (0.5 * (a - d) / den if den else 0.0)
        rates.append(60.0 * fs / k)
        strengths.append(float(r[int(round(k))]))
    if not rates:
        return None, None
    return float(np.median(rates)), float(np.median(strengths))


def _beats(y, mask, fs, c):
    """Unguided systolic-peak detection on the inverted IR pulse.

    Returns contiguous inter-beat intervals (s); detection does not use the
    spectral estimate, so agreement between the two is an independent check.
    """
    x = -y
    if not mask.any():
        return np.array([])
    amp = np.subtract(*np.percentile(x[mask], [90, 10]))
    if amp <= 0:
        return np.array([])
    peaks, _ = signal.find_peaks(x, distance=max(1, int(fs * 60 / c.max_bpm)), prominence=0.35 * amp)
    peaks = peaks[mask[peaks]]
    if len(peaks) < 4:
        return np.array([])
    refined = []
    for p in peaks:
        if 0 < p < len(x) - 1:
            a, b, d = x[p - 1], x[p], x[p + 1]
            den = a - 2 * b + d
            refined.append(p + (0.5 * (a - d) / den if den else 0.0))
        else:
            refined.append(float(p))
    ibi = np.diff(np.asarray(refined) / fs)
    contiguous = np.array([mask[int(peaks[i]):int(peaks[i + 1]) + 1].all() for i in range(len(peaks) - 1)])
    ok = contiguous & (ibi >= 60 / c.max_bpm) & (ibi <= 60 / c.min_bpm)
    return np.where(ok, ibi, np.nan)


def estimate_heart_rate(channels, fs, config: EstimatorConfig | None = None):
    """Estimate pulse rate for one recording. Returns a JSON-friendly dict."""
    c = config or EstimatorConfig()
    out = dict(version=VERSION, heart_rate_bpm=None, grade="REJECT", reason=None,
               sqi=None, clean_fraction=0.0, windows=0, window_agreement=None,
               red_bpm=None, red_agreement=None, beat_bpm=None, beats=0,
               rmssd_ms=None, sdnn_ms=None, perfusion_index=None, ir_dc=None,
               window_bpm_iqr=None, beat_regularity=None, acf_bpm=None, acf_strength=None,
               method_votes=0, config=asdict(c))
    ir = np.asarray(channels["ir"], float)
    if len(ir) < 30 * fs:
        out["reason"] = "RECORD_TOO_SHORT"
        return out
    dc = float(np.median(ir))
    out["ir_dc"] = dc
    if dc < c.minimum_dc:
        out["reason"] = "NO_SKIN_CONTACT"
        return out
    if np.mean(ir >= c.saturation_counts) > 0.01:
        out["reason"] = "SATURATED"
        return out
    y = _bandpass(ir, fs, c)
    mask, clean = _clean_mask(y, fs, c)
    out["clean_fraction"] = round(clean, 4)
    if mask.any():
        out["perfusion_index"] = float(np.std(y[mask]) / dc)
    if clean < c.minimum_clean_fraction:
        out["reason"] = "MOTION_ARTIFACT"
        return out
    rows = _window_estimates(y, mask, fs, c)
    out["windows"] = len(rows)
    if len(rows) < c.minimum_windows:
        out["reason"] = "TOO_FEW_CLEAN_WINDOWS"
        return out
    bpm = np.array([r[1] for r in rows])
    sqi = np.array([r[2] for r in rows])
    centre = float(np.median(bpm))
    agree = np.abs(bpm - centre) <= c.agreement_bpm
    agreement = float(agree.mean())
    hr = float(np.average(bpm[agree], weights=sqi[agree])) if agree.any() else centre
    q75, q25 = np.percentile(bpm, [75, 25])
    out.update(heart_rate_bpm=round(hr, 2), sqi=round(float(np.median(sqi)), 4),
               window_agreement=round(agreement, 4), window_bpm_iqr=round(float(q75 - q25), 2))
    ibi = _beats(y, mask, fs, c)
    valid = ibi[np.isfinite(ibi)] if len(ibi) else ibi
    regular = 0.0
    if len(valid) >= 10:
        med = float(np.median(valid))
        near = np.isfinite(ibi) & (np.abs(ibi - med) <= 0.12 * med)
        regular = float(near.sum() / max(1, len(valid)))
        out["beat_bpm"] = round(60.0 / med, 2)
        out["beats"] = int(len(valid))
        out["beat_regularity"] = round(regular, 4)
        clean_ibi = np.where(near, ibi, np.nan)
        pairs = np.diff(clean_ibi)
        pairs = pairs[np.isfinite(pairs)]
        kept = clean_ibi[np.isfinite(clean_ibi)]
        if len(kept) >= 30 and len(pairs) >= 20 and regular >= 0.7:
            out["rmssd_ms"] = round(float(np.sqrt(np.mean(pairs ** 2)) * 1000), 2)
            out["sdnn_ms"] = round(float(np.std(kept, ddof=1) * 1000), 2)
    acf_bpm, acf_r = _acf_rate(y, mask, fs, c)
    out["acf_bpm"] = round(acf_bpm, 2) if acf_bpm is not None else None
    out["acf_strength"] = round(acf_r, 4) if acf_r is not None else None
    if "red" in channels and len(channels["red"]) == len(ir) and np.median(channels["red"]) >= c.minimum_dc:
        yr = _bandpass(np.asarray(channels["red"], float), fs, c)
        red_rows = _window_estimates(yr, mask, fs, c)
        if len(red_rows) >= c.minimum_windows:
            rb = float(np.median([r[1] for r in red_rows]))
            out["red_bpm"] = round(rb, 2)
            out["red_agreement"] = bool(abs(rb - hr) <= c.agreement_bpm)
    beat_ok = out["beat_bpm"] is not None and abs(out["beat_bpm"] - hr) <= 4.0
    acf_ok = acf_bpm is not None and abs(acf_bpm - hr) <= 4.0
    votes = int(beat_ok) + int(acf_ok) + int(bool(out["red_agreement"]))
    out["method_votes"] = votes
    if agreement >= 0.7 and out["sqi"] >= 0.30 and beat_ok and acf_ok and regular >= 0.6:
        out["grade"] = "HIGH"
    elif agreement >= 0.5 and out["sqi"] >= 0.20 and votes >= 2:
        out["grade"] = "MEDIUM"
    else:
        out["grade"] = "LOW"
        out["reason"] = "INCONSISTENT_RATE"
    return out


def estimate_document(document, config: EstimatorConfig | None = None):
    fs, duration, channels, meta = decode_record(document)
    result = estimate_heart_rate(channels, fs, config)
    result.update(sample_rate_hz=fs, duration_s=round(duration, 3), channels=sorted(channels), **meta)
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in result.items()}
