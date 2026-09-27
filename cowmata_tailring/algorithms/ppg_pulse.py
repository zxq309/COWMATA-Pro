"""PPG pulse-wave (脉诊) measurement for the calving decision · ``ppg-pulse-1``.

One 90 s tail-ring PPG capture (two optical channels, Base64 uint32) becomes one
measurement with a signal-quality index and the pulse-wave information that the
``heart_rate`` and ``spo2`` modules do not already provide:

* autonomic tone from the pulse-interval series (RMSSD, SD1/SD2, rhythm irregularity);
* PPG-derived respiration (AM/BW/FM modulation, Charlton 2016 style fusion);
* Wang Wei-Kung harmonic analysis of the median beat: total harmonic perfusion ``C0``
  (geometric mean of the first six 1000·An/DC) and gain-free harmonic shape ratios
  ``Cn/C0`` (C1 肝经 … C5 胃经), plus beat-to-beat harmonic stability ``CVn``;
* contour morphology (rise-time ratio, reflection index, SDPTG ageing index).

The decision engine only reads windowed statistics of these values (see
``cowmata_engine.features.ppg_pulse``); the TCM interpretation layer (meridians,
syndrome elements, health map) is a research display and never feeds the calving model.
No Qt, no labels, no ledger: this module only sees one capture at a time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt, welch

from .heart_rate import measurement_timing

VERSION = "ppg-pulse-1"
N_BEAT = 256
MAX_ORDER = 11
QUALITY_SQI = 60.0
ABSTAIN_SQI = 45.0
MERIDIANS = ("心包经", "肝经", "肾经", "脾经", "肺经", "胃经", "胆经", "膀胱经", "大肠经", "三焦经", "小肠经", "心经")


def _f(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def decode_u32(value) -> np.ndarray:
    if not value:
        return np.empty(0, dtype=float)
    raw = base64.b64decode(value)
    if len(raw) % 4:
        raise ValueError("PPG 字节数不是 4 的整数倍")
    return np.frombuffer(raw, dtype="<u4").astype(np.float64)


def _configs(document):
    cfg = document.get("configs") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _bandpass(x, fs, lo, hi, order=3):
    sos = butter(order, [lo, min(hi, fs * 0.45)], btype="bandpass", fs=fs, output="sos")
    return sosfiltfilt(sos, x - np.median(x))


def _robust_scale(x):
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826
    return mad if mad > 1e-12 else float(np.std(x)) + 1e-12


def _spectral_concentration(y, fs):
    f, p = welch(y, fs=fs, nperseg=min(len(y), 2048))
    band = (f >= 0.5) & (f <= 5.0)
    cardiac = (f >= 0.7) & (f <= 3.0)
    if not np.any(cardiac):
        return 0.0, None
    f0 = float(f[cardiac][np.argmax(p[cardiac])])
    near = (np.abs(f - f0) <= 0.12) | (np.abs(f - 2 * f0) <= 0.18)
    return float(np.sum(p[near & band]) / (np.sum(p[band]) + 1e-12)), 60.0 * f0


def _polarity(y):
    d = np.diff(y)
    return 1 if np.percentile(d, 99.5) >= -np.percentile(d, 0.5) else -1


def _fiducials(y, fs):
    peaks, _ = find_peaks(y, distance=max(1, int(fs * 60.0 / 150.0)), prominence=max(0.3 * np.std(y), 1e-9))
    if len(peaks) < 4:
        return np.empty(0, int), np.empty(0, int), np.empty(0)
    ibi = float(np.median(np.diff(peaks)))
    d1 = np.gradient(y)
    feet, slope_t, kept = [], [], []
    for p in peaks:
        a = int(max(0, p - 0.6 * ibi))
        if p - a < 3:
            continue
        foot = a + int(np.argmin(y[a:p]))
        if p - foot < 2:
            continue
        k = foot + int(np.argmax(d1[foot:p]))
        off = 0.0
        if 0 < k < len(d1) - 1:
            den = d1[k - 1] - 2 * d1[k] + d1[k + 1]
            off = float(np.clip(0.5 * (d1[k - 1] - d1[k + 1]) / den, -0.5, 0.5)) if abs(den) > 1e-12 else 0.0
        feet.append(foot)
        slope_t.append((k + off) / fs)
        kept.append(p)
    return np.asarray(kept, int), np.asarray(feet, int), np.asarray(slope_t)


def _hrv(slope_t):
    out = dict(rmssd_ms=None, sdnn_ms=None, sd1sd2=None, irregularity=None, pulse_rate_bpm=None, ibi_ms=[])
    if len(slope_t) < 5:
        return out
    ibi = np.diff(slope_t)
    med = float(np.median(ibi))
    ok = (ibi > 0.4) & (ibi < 1.8) & (np.abs(ibi - med) < 0.3 * med)
    ms = ibi[ok] * 1000.0
    if len(ms) < 5:
        return out
    d = np.diff(ms)
    good = np.abs(d) <= 0.2 * ms[:-1]  # Malik 20 % rule
    dd = d[good] if np.sum(good) >= 3 else d
    sdnn = float(np.std(ms, ddof=1))
    sd1 = float(np.sqrt(0.5) * np.std(dd, ddof=1)) if len(dd) > 2 else None
    sd2 = float(np.sqrt(max(2 * sdnn ** 2 - (sd1 or 0) ** 2, 1e-9))) if sd1 is not None else None
    out.update(rmssd_ms=float(np.sqrt(np.mean(dd ** 2))), sdnn_ms=sdnn,
               sd1sd2=(sd1 / sd2) if sd1 is not None and sd2 else None,
               irregularity=float(np.mean(~good)) if len(d) else 0.0,
               pulse_rate_bpm=60000.0 / float(np.median(ms)), ibi_ms=[round(float(v), 1) for v in ms])
    return out


def _respiration(beat_t, amp, base):
    if len(beat_t) < 20 or beat_t[-1] - beat_t[0] < 40:
        return None, None
    grid = np.arange(beat_t[0], beat_t[-1], 0.25)
    estimates = []
    for series, times in ((amp, beat_t), (base, beat_t), (np.diff(beat_t), beat_t[1:])):
        if len(series) < 20 or len(series) != len(times):
            continue
        s = np.interp(grid, times, series)
        f, p = welch(s - s.mean(), fs=4.0, nperseg=min(len(s), 256))
        band = (f >= 0.15) & (f <= 1.0)
        if not np.any(band) or np.sum(p[band]) <= 0:
            continue
        i = int(np.argmax(p[band]))
        estimates.append((float(f[band][i] * 60.0), float(p[band][i] / (np.mean(p[band]) + 1e-12))))
    if not estimates:
        return None, None
    rates = np.array([e[0] for e in estimates])
    weights = np.array([e[1] for e in estimates])
    agreement = float(1.0 - min(1.0, np.std(rates) / (np.mean(rates) + 1e-9)))
    return (float(np.sum(rates * weights) / np.sum(weights)) if agreement >= 0.6 else None), agreement


def _morphology(template):
    n = len(template)
    spec = np.fft.rfft(template - np.mean(template))
    keep = np.zeros_like(spec)
    keep[1:11] = spec[1:11]
    smooth = np.fft.irfft(keep, n=n)
    d2 = np.fft.irfft(keep * (-(2 * np.pi * np.arange(len(spec)) / n) ** 2), n=n)
    y = (smooth - smooth.min()) / (np.ptp(smooth) + 1e-12)
    p = int(np.argmax(y))
    result = dict(rise_ratio=p / n, width50=float(np.mean(y > 0.5)), reflection_index=None, ageing_index=None)
    ext = [(i, "max" if d2[i] > d2[i - 1] and d2[i] >= d2[i + 1] else "min") for i in range(1, int(0.7 * n) - 1)
           if (d2[i] > d2[i - 1] and d2[i] >= d2[i + 1]) or (d2[i] < d2[i - 1] and d2[i] <= d2[i + 1])]
    seq, want = [int(np.argmax(d2[: max(3, p + 1)]))], "min"
    for i, kind in ext:
        if i > seq[-1] and kind == want:
            seq.append(i)
            want = "max" if want == "min" else "min"
        if len(seq) == 5:
            break
    if len(seq) == 5 and d2[seq[0]] > 1e-9:
        a, b, c, d, e = (float(d2[i]) for i in seq)
        result["ageing_index"] = (b - c - d - e) / a
        if seq[4] > p:
            result["reflection_index"] = float(y[seq[4]])
    if result["reflection_index"] is None:
        tail = d2[p + 3: int(0.85 * n)]
        if len(tail) > 3:
            result["reflection_index"] = float(y[p + 3 + int(np.argmax(tail))])
    result["template"] = [round(float(v), 4) for v in y]
    return result


def analyse_document(document, *, polarity=None):
    """Signal-level analysis of one PPG document (no timing, no identity)."""
    cfg = _configs(document)
    channels = [(k, decode_u32(document.get(k))) for k in ("data", "ir_data")]
    channels = [(k, v) for k, v in channels if len(v) > 200]
    if not channels:
        raise ValueError("PPG 记录没有可用通道")
    n = max(len(v) for _, v in channels)
    duration = float(cfg.get("pulse_sample_time") or 0)
    fs = n / duration if duration > 0 else 100.0
    fused, concentration, hr_psd = [], {}, None
    for name, raw in channels:
        y = _bandpass(raw, fs, 0.5, 5.0)
        conc, hr = _spectral_concentration(y, fs)
        concentration[name] = conc
        hr_psd = hr_psd or hr
        fused.append((max(conc, 1e-3), y / (_robust_scale(y) + 1e-12)))
    weights = np.array([w for w, _ in fused])
    signal = np.sum([w * s for w, (_, s) in zip(weights / weights.sum(), fused)], axis=0)
    auto = _polarity(_bandpass(channels[0][1], fs, 0.3, 20.0))
    pol = polarity if polarity in (1, -1) else auto
    peaks, feet, slope_t = _fiducials(pol * signal, fs)
    beats = []
    if len(feet) >= 5:
        med = float(np.median(np.diff(feet)))
        beats = [(int(feet[i]), int(feet[i + 1]), int(peaks[i])) for i in range(len(feet) - 1)
                 if 0.35 * fs <= feet[i + 1] - feet[i] <= 2.0 * fs and abs(feet[i + 1] - feet[i] - med) < 0.35 * med]
    per = {}
    for name, raw in channels:
        if len(beats) < 8:
            continue
        yh = pol * _bandpass(raw, fs, 0.3, 20.0)
        dc = float(np.mean(raw))
        amps, bases, shapes, harm = [], [], [], []
        for a, b, pk in beats:
            seg = yh[a:b + 1]
            seg = seg - np.linspace(seg[0], seg[-1], len(seg))
            beat = np.interp(np.linspace(0, len(seg) - 1, N_BEAT, endpoint=False), np.arange(len(seg)), seg)
            spectrum = np.fft.rfft(beat) / N_BEAT
            harm.append(1000.0 * 2.0 * np.abs(spectrum[1:MAX_ORDER + 1]) / max(dc, 1e-9))
            shapes.append(beat)
            amps.append(float(yh[pk] - yh[a]))
            bases.append(float(raw[a]))
        S, H = np.asarray(shapes), np.asarray(harm)
        Sn = (S - S.mean(axis=1, keepdims=True)) / (S.std(axis=1, keepdims=True) + 1e-12)
        corr = np.array([np.corrcoef(s, np.median(Sn, axis=0))[0, 1] for s in Sn])
        keep = np.isfinite(corr) & (corr >= 0.8)
        if keep.sum() < 8:
            keep = np.isfinite(corr) & (corr >= np.nanpercentile(corr, 30))
        Hk = H[keep]
        per[name] = dict(C=Hk.mean(axis=0), CV=100.0 * Hk.std(axis=0, ddof=1) / (Hk.mean(axis=0) + 1e-12),
                         accepted=int(keep.sum()), corr=float(np.nanmean(corr[keep])), template=np.median(S[keep], axis=0),
                         pi=100.0 * float(np.median(np.asarray(amps)[keep])) / dc, amps=np.asarray(amps), bases=np.asarray(bases))
    expected = (n / fs) * (hr_psd or 70.0) / 60.0
    sqi = {}
    for name, _ in channels:
        pc = per.get(name)
        cover = min(1.0, pc["accepted"] / max(expected, 1.0)) if pc else 0.0
        stability = float(np.clip(1.0 - (pc["CV"][0] - 20.0) / 80.0, 0.0, 1.0)) if pc else 0.0
        sqi[name] = 100.0 * (0.25 * concentration[name] + 0.35 * cover + 0.2 * (max(pc["corr"], 0.0) if pc else 0.0)
                             + 0.2 * stability)
    usable = [k for k in per if sqi[k] >= 50] or ([max(per, key=lambda k: sqi[k])] if per else [])
    result = dict(fs_hz=fs, duration_s=n / fs, polarity=pol, polarity_auto=auto, beats=len(beats),
                  expected_beats=expected, sqi=max(sqi.values()) if sqi else 0.0, sqi_channels=sqi)
    hrv = _hrv(slope_t)
    result.update(hrv)
    if not usable:
        return result
    C = np.mean([per[k]["C"] for k in usable], axis=0)
    CV = np.mean([per[k]["CV"] for k in usable], axis=0)
    c0 = float(np.exp(np.mean(np.log(np.maximum(C[:6], 1e-9)))))
    result.update(harmonic_c0=c0, harmonic_ratio=[float(v / c0) for v in C], harmonic_cv=[float(v) for v in CV],
                  harmonic_cv_mid=float(np.mean(CV[3:6])), template_corr=max(per[k]["corr"] for k in usable),
                  perfusion_index=float(np.mean([per[k]["pi"] for k in usable])))
    result.update(_morphology(np.mean([per[k]["template"] for k in usable], axis=0)))
    best = per[usable[0]]
    bt = np.array([a / fs for a, _, _ in beats])
    result["resp_rate_bpm"], result["resp_agreement"] = _respiration(bt, best["amps"], best["bases"])
    return result


def measure_document(document, *, source="", source_sha256=None, polarity=None):
    sample, available, basis = measurement_timing(document)
    analysis = analyse_document(document, polarity=polarity)
    sqi = float(analysis.pop("sqi", 0.0) or 0.0)
    return dict(schema=VERSION, source=str(source), source_sha256=source_sha256,
                device_id=str(document.get("device") or "").upper(), sample_epoch_ms=int(sample),
                available_epoch_ms=int(max(available, sample + 1000 * analysis.get("duration_s", 0))),
                time_basis=basis, sqi=sqi, quality=sqi >= QUALITY_SQI,
                gate="pass" if sqi >= QUALITY_SQI else ("warn" if sqi >= ABSTAIN_SQI else "abstain"), **analysis)


def measure_file(path, *, polarity=None):
    content = Path(path).read_bytes()
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("imu") or not (document.get("data") or document.get("ir_data")):
        raise ValueError("不是 PPG JSON")
    return measure_document(document, source=str(path), source_sha256=hashlib.sha256(content).hexdigest(),
                            polarity=polarity)