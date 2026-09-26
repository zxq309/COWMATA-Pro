"""Red/IR reflectance-PPG oximetry for tail rings: one quality-gated SpO2 per record.

Method (ratio of ratios, window gated):
  DC = 2 s running median; normalised AC = band-pass 0.7-3.5 Hz of x/DC-1.
  8 s windows, 2 s step. A window is kept only if red and IR pulses are the same
  cardiac rhythm (AC correlation, autocorrelation periodicity, heart-rate agreement),
  there is no dropout/clipping/motion step, and perfusion is physiological.
  R = rms(AC_red)/rms(AC_ir) (both DC-normalised); SpO2 = device reference curve
  of the MAX3010x red/IR front end: -45.060 R^2 + 30.354 R + 94.845 (Maxim RD117).
The curve is the sensor vendor's human calibration and has not been re-fitted
against bovine arterial blood gas, so the absolute percentage is an estimate;
R, record-to-record change and the cow's own baseline carry the decision value.
"""
from __future__ import annotations

import numpy as np

VERSION = "spo2-ratio-gated-1"
CALIBRATION = dict(name="maxim-rd117-quadratic", a=-45.060, b=30.354, c=94.845,
                   note="MAX3010x 厂商参考曲线；未用牛动脉血气重新标定")
ADC_FULL_SCALE = 262143          # 18-bit MAX3010x FIFO
WINDOW_S, STEP_S = 8.0, 2.0
BAND_HZ = (0.7, 3.5)
HR_RANGE_BPM = (40.0, 150.0)
R_RANGE = (0.20, 1.20)           # outside: artefact or implausible for a standing/lying adult cow
SPO2_RANGE = (70.0, 100.0)
GATES = dict(min_ac_correlation=0.80, min_periodicity=0.45, max_hr_disagreement_bpm=8.0,
             perfusion_index_percent=(0.02, 6.0), max_dc_drift=0.03, max_accel_std_g=0.06,
             min_dc_adc=3000.0, min_valid_windows=3, good_valid_windows=8, good_r_cv=0.15)


# The quadratic peaks (≈99.96 %) at R≈0.337; below that the optical signal only says
# "saturated", so R is clamped to the vertex to keep the curve monotonic.
R_SATURATED = -CALIBRATION["b"] / (2 * CALIBRATION["a"])


def spo2_from_ratio(ratio):
    r = np.maximum(np.asarray(ratio, dtype=float), R_SATURATED)
    value = CALIBRATION["a"] * r * r + CALIBRATION["b"] * r + CALIBRATION["c"]
    return np.minimum(value, 100.0)


def _running_median(x, width):
    from scipy.ndimage import median_filter
    return median_filter(x, size=max(3, int(width) | 1), mode="nearest")


def _band(x, fs):
    from scipy.signal import butter, sosfiltfilt
    sos = butter(3, [BAND_HZ[0], min(BAND_HZ[1], 0.45 * fs)], btype="band", fs=fs, output="sos")
    return sosfiltfilt(sos, x)


def _periodicity(ac, fs):
    """Normalised autocorrelation peak within the cardiac lag range -> (strength, bpm)."""
    ac = ac - ac.mean()
    energy = float(np.dot(ac, ac))
    if energy <= 0:
        return 0.0, None
    lo, hi = int(fs * 60 / HR_RANGE_BPM[1]), int(fs * 60 / HR_RANGE_BPM[0])
    full = np.correlate(ac, ac, mode="full")[len(ac) - 1:]
    hi = min(hi, len(full) - 2)
    if hi <= lo:
        return 0.0, None
    seg = full[lo:hi + 1] / energy
    k = int(np.argmax(seg))
    # A pulse train also correlates at 2T, 3T ...; take the shortest lag whose local
    # peak reaches 80 % of the best one so fast rhythms are not halved.
    peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
    if peaks and seg[k] > 0:
        k = next((i for i in peaks if seg[i] >= 0.8 * seg[k]), k)
    # parabolic refinement of the lag
    lag = lo + k
    if 0 < k < len(seg) - 1:
        a, b, c = seg[k - 1], seg[k], seg[k + 1]
        denom = a - 2 * b + c
        if denom:
            lag += 0.5 * (a - c) / denom
    return float(seg[k]), float(60.0 * fs / lag)


def _dropouts(x, dc):
    dev = x / np.maximum(dc, 1.0) - 1.0
    mad = float(np.median(np.abs(dev - np.median(dev)))) * 1.4826
    limit = max(0.004, 8.0 * mad)
    return (np.abs(dev) > limit) | (x <= 0) | (x >= ADC_FULL_SCALE - 64)


def analyse_signal(red, ir, fs, accel=None):
    """Return record-level oximetry and per-window diagnostics.

    ``red`` may be None (IR-only records keep heart rate but no SpO2).
    ``accel`` is an optional (n, 3) array in g sampled with the optical channels.
    """
    ir = np.asarray(ir, dtype=float)
    red = None if red is None else np.asarray(red, dtype=float)
    n = len(ir)
    width, step = int(round(WINDOW_S * fs)), int(round(STEP_S * fs))
    result = dict(algorithm=VERSION, calibration=CALIBRATION["name"], sample_rate_hz=float(fs),
                  samples=n, windows_total=0, windows_valid=0, windows_pulse=0,
                  ratio_r=None, ratio_r_iqr=None, spo2_percent=None, heart_rate_bpm=None,
                  perfusion_index_percent=None, red_dc=None, ir_dc=None, quality="insufficient",
                  reject_reasons={}, windows=[])
    if n < width or fs <= 0:
        result["reject_reasons"] = {"record_too_short": 1}
        return result
    dc_i = _running_median(ir, 2 * fs)
    bad = _dropouts(ir, dc_i)
    ac_i = _band(ir / np.maximum(dc_i, 1.0) - 1.0, fs)
    if red is not None:
        dc_r = _running_median(red, 2 * fs)
        bad |= _dropouts(red, dc_r)
        ac_r = _band(red / np.maximum(dc_r, 1.0) - 1.0, fs)
    grow = int(0.5 * fs)
    if bad.any():
        from scipy.ndimage import binary_dilation
        bad = binary_dilation(bad, iterations=grow)
    amag = None
    if accel is not None and len(accel) == n:
        amag = np.linalg.norm(np.asarray(accel, dtype=float), axis=1)
    reasons = {}
    windows = []
    for start in range(0, n - width + 1, step):
        sl = slice(start, start + width)
        w = dict(start_s=round(start / fs, 2))
        reason = None
        di = dc_i[sl]
        drift = float((di.max() - di.min()) / max(di.mean(), 1.0))
        strength_i, bpm_i = _periodicity(ac_i[sl], fs)
        pi_i = float(np.percentile(ac_i[sl], 95) - np.percentile(ac_i[sl], 5)) * 100
        x = ac_i[sl]
        skew = float(np.mean((x - x.mean()) ** 3) / max(np.std(x) ** 3, 1e-18))
        w.update(ir_dc=float(di.mean()), drift=round(drift, 4), periodicity_ir=round(strength_i, 3), skewness_ir=round(skew, 3),
                 hr_ir=None if bpm_i is None else round(bpm_i, 1), pi_ir=round(pi_i, 4))
        if bad[sl].any():
            reason = "dropout_or_clipping"
        elif di.mean() < GATES["min_dc_adc"]:
            reason = "no_skin_contact"
        elif amag is not None and float(np.std(amag[sl])) > GATES["max_accel_std_g"]:
            reason = "motion_accelerometer"
        elif drift > GATES["max_dc_drift"]:
            reason = "motion_dc_step"
        elif strength_i < GATES["min_periodicity"] or bpm_i is None:
            reason = "no_cardiac_rhythm"
        elif not (HR_RANGE_BPM[0] <= bpm_i <= HR_RANGE_BPM[1]):
            reason = "heart_rate_out_of_range"
        elif not (GATES["perfusion_index_percent"][0] <= pi_i <= GATES["perfusion_index_percent"][1]):
            reason = "perfusion_out_of_range"
        if reason is None:
            w["pulse_ok"] = True
        if reason is None and red is not None:
            ar, ai = ac_r[sl], ac_i[sl]
            corr = float(np.corrcoef(ar, ai)[0, 1])
            strength_r, bpm_r = _periodicity(ar, fs)
            ratio = float(np.std(ar) / max(np.std(ai), 1e-12))
            w.update(red_dc=float(dc_r[sl].mean()), corr=round(corr, 3), periodicity_red=round(strength_r, 3),
                     hr_red=None if bpm_r is None else round(bpm_r, 1), ratio_r=round(ratio, 4))
            if float(dc_r[sl].mean()) < GATES["min_dc_adc"]:
                reason = "no_skin_contact"
            elif corr < GATES["min_ac_correlation"]:
                reason = "red_ir_incoherent"
            elif bpm_r is None or abs(bpm_r - bpm_i) > GATES["max_hr_disagreement_bpm"]:
                reason = "red_ir_rate_mismatch"
            elif not (R_RANGE[0] <= ratio <= R_RANGE[1]):
                reason = "ratio_implausible"
        elif reason is None:
            reason = "no_red_channel"
        w["reason"] = reason
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
        windows.append(w)
    pulse = [w for w in windows if w.get("pulse_ok")]
    valid = [w for w in windows if w["reason"] is None]
    result.update(windows_total=len(windows), windows_valid=len(valid), windows_pulse=len(pulse),
                  reject_reasons=reasons, windows=windows, ir_dc=float(np.median(ir)),
                  red_dc=None if red is None else float(np.median(red)))
    if pulse:
        result["heart_rate_bpm"] = round(float(np.median([w["hr_ir"] for w in pulse])), 1)
        result["perfusion_index_percent"] = round(float(np.median([w["pi_ir"] for w in pulse])), 4)
    if len(valid) >= GATES["min_valid_windows"]:
        ratios = np.array([w["ratio_r"] for w in valid])
        med = float(np.median(ratios))
        iqr = float(np.percentile(ratios, 75) - np.percentile(ratios, 25))
        spo2 = float(spo2_from_ratio(med))
        result.update(ratio_r=round(med, 4), ratio_r_iqr=round(iqr, 4))
        if SPO2_RANGE[0] <= spo2 <= SPO2_RANGE[1]:
            result["spo2_percent"] = round(spo2, 2)
            cv = iqr / 1.349 / max(med, 1e-9)
            result["quality"] = "good" if len(valid) >= GATES["good_valid_windows"] and cv <= GATES["good_r_cv"] else "fair"
        else:
            result["quality"] = "implausible"
    return result


def analyse_ppg(ppg):
    """Analyse a parsed :class:`~cowmata_tailring.workspace.sensor_records.PPGData`."""
    channels = ppg.channels
    if "ppg_ir" not in channels:
        raise ValueError("PPG 缺少红外通道，不能计算血氧或心率")
    red = channels["ppg_primary"][2] if "ppg_primary" in channels else None
    accel = None
    if all(k in channels for k in ("ax", "ay", "az")):
        accel = np.column_stack([channels[k][2] for k in ("ax", "ay", "az")])
    return analyse_signal(red, channels["ppg_ir"][2], ppg.sample_rate_hz, accel)
