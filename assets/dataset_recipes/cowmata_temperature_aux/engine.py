# -*- coding: utf-8 -*-
"""Causal, gap-tolerant temperature evidence; predictions are never observations."""
import json, math, hashlib
from pathlib import Path
from dataclasses import dataclass, asdict
import numpy as np
from .protocol import Context, PacketError, ProtocolConfig, decode_packet
from .cooling import new_state, reset_run, update_cooling, cooling_status

VERSION = "temperature-aux-0.6.0"
HOUR = 3600000.0
MINUTE = 60000.0
FEATURE_NAMES = [
    "B_selected_reference_drop",
    "B_relative_rmssd_gain",
    "B_relative_width_gain",
    "B_absolute_variability_support",
]


def finite(x):
    if isinstance(x, dict):
        return {str(k): finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [finite(v) for v in x]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


@dataclass(frozen=True)
class Config:
    history_hours: float = 96.0
    min_c: float = 30.0
    max_c: float = 42.0
    observation_sd_c: float = 0.06
    process_level_sd_c_per_sqrt_min: float = 0.025
    process_slope_sd_cpm: float = 0.0015
    slope_damping_minutes: float = 20.0
    maximum_slope_c_per_minute: float = 0.08
    maximum_imu_gap_ms: float = 1000.0
    regime_drop_c: float = 4.0
    regime_min_points: int = 10
    rmssd_floor_c: float = 0.01
    width_floor_c: float = 0.05
    medium_score: float = 70.0
    high_score: float = 90.0
    clear_margin: float = 0.0
    hold_minutes: float = 60.0
    high_confirmations: int = 2
    clear_confirmations: int = 1
    maximum_confirmation_gap_minutes: float = 90.0
    maximum_observation_age_to_escalate_minutes: float = 15.0

    def __post_init__(self):
        if not 0 < self.medium_score < self.high_score <= 100:
            raise ValueError("grade thresholds must increase")
        if self.history_hours < 30 or self.min_c >= self.max_c:
            raise ValueError("invalid history/range")
        if self.high_confirmations < 1 or self.clear_confirmations < 1:
            raise ValueError("invalid confirmations")


def weighted_quantile(values, weights, q=0.5):
    values = np.asarray(values, float)
    weights = np.asarray(weights, float)
    ok = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not ok.any():
        return None
    v = values[ok]
    w = weights[ok]
    i = np.argsort(v, kind="mergesort")
    v = v[i]
    w = w[i]
    return float(
        np.interp(q * w.sum(), np.cumsum(w) - 0.5 * w, v, left=v[0], right=v[-1])
    )


def metric(t, y):
    """Actual adjacent pairs only. A missing minute is not a zero fluctuation."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    ok = np.isfinite(y)
    t = t[ok]
    y = y[ok]
    if len(y) < 8 or np.ptp(t) < 6 * MINUTE:
        return None
    pairs = (np.diff(t) > 0) & (np.diff(t) <= 2 * MINUTE)
    if pairs.sum() < 5:
        return None
    x = (t - t.mean()) / HOUR
    den = np.dot(x, x)
    if den <= 0:
        return None
    slope = float(np.dot(x, y - y.mean()) / den)
    res = y - y.mean() - slope * x
    return dict(
        rmssd_c=float(np.sqrt(np.mean(np.diff(res)[pairs] ** 2))),
        width_c=float(np.quantile(res, 0.9) - np.quantile(res, 0.1)),
        slope_cph=slope,
        pairs=int(pairs.sum()),
    )


def predict_filter(x, p, dt_minutes, c):
    if dt_minutes <= 0:
        return x.copy(), p.copy()
    decay = math.exp(-dt_minutes / c.slope_damping_minutes)
    f = np.array([[1.0, c.slope_damping_minutes * (1 - decay)], [0.0, decay]])
    q = np.diag(
        [
            c.process_level_sd_c_per_sqrt_min ** 2 * dt_minutes,
            c.process_slope_sd_cpm ** 2 * min(dt_minutes, c.slope_damping_minutes),
        ]
    )
    return f.dot(x), f.dot(p).dot(f.T) + q


def load_model():
    p = Path(__file__).with_name("model.json")
    return json.loads(p.read_text(encoding="utf-8"))


def score_model(features, model):
    names = model.get("feature_names", FEATURE_NAMES)
    if names != FEATURE_NAMES:
        raise ValueError("model feature schema mismatch")
    x = np.array([float(features[k]) for k in names])
    kind = model.get("kind")
    if kind == "nonnegative_ridge_logistic":
        coefficients = np.array(model["coef"], float)
        if len(coefficients) != len(names) or np.any(coefficients < 0):
            raise ValueError("invalid reference model coefficients")
        terms = x * coefficients
        z = float(terms.sum() + model["intercept"])
        score = round(100 / (1 + math.exp(-float(np.clip(z, -40, 40)))), 4)
        return (
            score,
            dict(
                kind=kind,
                linear_contributions=dict(zip(names, terms.tolist())),
                intercept=model["intercept"],
                logit=z,
            ),
        )
    # Explicit constant/test trees remain available for state-machine isolation tests.
    if kind == "tree":
        nodes = model["nodes"]
        i = 0
        while nodes[i]["feature"] >= 0:
            n = nodes[i]
            i = n["left"] if x[n["feature"]] <= n["threshold"] else n["right"]
        return round(float(nodes[i]["score"]), 4), {"kind": "tree", "leaf": i}
    raise ValueError("unsupported reference model kind")


class TemperatureModule:
    """One instance per cow/device binding. Always returns finite evidence/grade.

    Low means weak temperature evidence, not a low probability of calving.
    Only real newly received observations can promote a grade or confirm HIGH.
    """

    def __init__(self, context, config=None, model=None):
        self.context = context
        self.model = load_model() if model is None else model
        if config is None and self.model.get("decision_config"):
            config = Config(**self.model["decision_config"])
        self.config = config or Config()
        self.phase = "prepartum"
        self.cooling_state = new_state()
        self.samples = []  # [time, raw degree C, accepted, uid]
        self.estimates = []  # [time, filtered/forecast C, sd, real_update, weight]
        self.metrics = []
        self.seen = {}
        self.filter_x = None
        self.filter_p = None
        self.filter_time = None
        self.last_real_time = None
        self.last_real_value = None
        self.last_clock = None
        self.last_end = None
        self.last_output = None
        self.last_packet = None
        self.cold_reference = None
        self.level = "LOW"
        self.high_run = 0
        self.clear_run = 0
        self.last_positive = None
        self.last_real_decision = None

    @property
    def config_hash(self):
        return hashlib.sha256(
            json.dumps(
                asdict(self.config), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @property
    def model_hash(self):
        return hashlib.sha256(
            json.dumps(self.model, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _clock(self, now):
        if type(now) is not int or now <= 0:
            raise ValueError("Unix ms integer required")
        if self.last_clock is not None and now < self.last_clock:
            raise ValueError("evaluation clock moved backwards")

    def _gap_estimate(self, predicted, t):
        """Short gaps: last actual value. 15-30 min: blend into bounded trend."""
        if self.last_real_time is None or self.last_real_value is None:
            return float(predicted)
        age = (t - self.last_real_time) / MINUTE
        blend = float(np.clip((age - 15.0) / 15.0, 0, 1))
        return float((1 - blend) * self.last_real_value + blend * predicted)

    def _filter(self, t, value, real):
        c = self.config
        if self.filter_x is None:
            if not real:
                return
            self.filter_x = np.array([float(value), 0.0])
            self.filter_p = np.diag([c.observation_sd_c ** 2, 0.0025])
            self.filter_time = float(t)
            self.last_real_time = float(t)
            self.last_real_value = float(value)
        else:
            dt = (t - self.filter_time) / MINUTE
            if dt <= 0:
                return
            # Explicit forecast points make short acquisition gaps reviewable.
            left = self.filter_time
            for ti in np.arange(left + MINUTE, t - 0.1, MINUTE):
                self.filter_x, self.filter_p = predict_filter(
                    self.filter_x, self.filter_p, (ti - self.filter_time) / MINUTE, c
                )
                self.filter_time = float(ti)
                age = (ti - self.last_real_time) / MINUTE
                self.estimates.append(
                    [
                        float(ti),
                        self._gap_estimate(self.filter_x[0], ti),
                        float(np.sqrt(self.filter_p[0, 0])),
                        False,
                        float(0.35 * math.exp(-age / 30.0)),
                    ]
                )
            self.filter_x, self.filter_p = predict_filter(
                self.filter_x, self.filter_p, (t - self.filter_time) / MINUTE, c
            )
            self.filter_time = float(t)
            if real:
                r = c.observation_sd_c ** 2
                s = self.filter_p[0, 0] + r
                k = self.filter_p[:, 0] / s
                self.filter_x = self.filter_x + k * (value - self.filter_x[0])
                # Joseph covariance update preserves numerical positive semidefiniteness.
                a = np.eye(2) - np.outer(k, [1.0, 0.0])
                self.filter_p = a.dot(self.filter_p).dot(a.T) + np.outer(k, k) * r
                self.filter_x[1] = np.clip(
                    self.filter_x[1],
                    -c.maximum_slope_c_per_minute,
                    c.maximum_slope_c_per_minute,
                )
                self.last_real_time = float(t)
                self.last_real_value = float(value)
        age = (t - self.last_real_time) / MINUTE
        estimate = (
            float(self.filter_x[0]) if real else self._gap_estimate(self.filter_x[0], t)
        )
        self.estimates.append(
            [
                float(t),
                estimate,
                float(np.sqrt(self.filter_p[0, 0])),
                bool(real),
                1.0 if real else float(0.35 * math.exp(-age / 30.0)),
            ]
        )

    def process_packet(
        self,
        obj,
        *,
        received_at_ms,
        evaluated_at_ms,
        receive_time_source="server_received",
    ):
        self._clock(evaluated_at_ms)
        if (
            type(received_at_ms) is not int
            or received_at_ms <= 0
            or received_at_ms > evaluated_at_ms
        ):
            raise ValueError("invalid receipt time")
        if receive_time_source not in ["server_received", "json_update_proxy"]:
            raise ValueError("invalid receipt source")
        p = decode_packet(obj, self.context, ProtocolConfig(), received_at_ms)
        if p["uid"] in self.seen:
            if p["digest"] != self.seen[p["uid"]]:
                raise PacketError("DUPLICATE_CONFLICT", "same uid different signal")
            out = self.snapshot(evaluated_at_ms)
            out["ingest_status"] = "DUPLICATE_IGNORED"
            return out
        if self.last_end is not None and p["create_ms"] <= self.last_end:
            out = self.snapshot(evaluated_at_ms)
            out["ingest_status"] = "LATE_OR_OVERLAP_IGNORED"
            return out
        c = self.config
        t = p["times"]
        y = p["values"]
        reasons = []
        good = (y >= c.min_c) & (y <= c.max_c)
        if not good.all():
            reasons.append("RANGE_OR_SCALE_REVIEW_RAW_RETAINED")
        timing_review = p["imu_max_dt_ms"] > c.maximum_imu_gap_ms
        if timing_review:
            good[:] = False
            reasons.append("TIMING_REVIEW_FORECAST_CONTINUES")
        prior = [s[1] for s in self.samples if s[2] and t[0] - 3 * HOUR <= s[0] < t[0]]
        quarantine = self.cold_reference is not None
        if not quarantine and len(prior) >= 20:
            reference = float(np.quantile(prior, 0.9))
            run = 0
            for low in y < reference - c.regime_drop_c:
                run = run + 1 if low else 0
                if run >= c.regime_min_points:
                    self.cold_reference = reference
                    quarantine = True
                    break
        if quarantine:
            good[:] = False
            reasons.append("LARGE_DOWNWARD_REGIME_REVIEW")
            if np.all(y[-min(5, len(y)) :] >= self.cold_reference - 2):
                self.cold_reference = None
                reasons.append("RECOVERY_NEXT_PACKET")
        if len(y) >= 10:
            med = float(np.median(y[good])) if good.any() else None
            if med is not None:
                isolated = good & (np.abs(y - med) > 3.0)
                if 0 < isolated.sum() < 3:
                    good[isolated] = False
                    reasons.append("ISOLATED_EXTREME_REVIEW")
        for ti, yi, ok in zip(t, y, good):
            self.samples.append([float(ti), float(yi), bool(ok), p["uid"]])
            self._filter(float(ti), float(yi), bool(ok))
        self.seen[p["uid"]] = p["digest"]
        self.last_end = p["end_ms"]
        self.last_clock = evaluated_at_ms
        cutoff = p["end_ms"] - c.history_hours * HOUR
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        self.estimates = [s for s in self.estimates if s[0] >= cutoff]
        self.metrics = [s for s in self.metrics if s["end_ms"] >= cutoff]
        a = np.array([[s[0], s[1]] for s in self.samples if s[2]], float).reshape(-1, 2)
        z = a[:, 0] >= p["end_ms"] - HOUR
        m = metric(a[z, 0], a[z, 1]) if good.sum() >= 8 else None
        if m:
            self.metrics.append(
                dict(
                    end_ms=p["end_ms"], received_at_ms=received_at_ms, uid=p["uid"], **m
                )
            )
        features, details = self._features(p["end_ms"], t, y, good)
        source = (
            "REAL_UPDATED"
            if good.sum() >= 8
            else "ESTIMATE_UPDATED"
            if self.filter_x is not None
            else "INITIAL_PRIOR"
        )
        age = (
            (evaluated_at_ms - self.last_real_time) / MINUTE
            if self.last_real_time is not None
            else None
        )
        reliable_new = (
            good.sum() >= 8
            and age is not None
            and age <= c.maximum_observation_age_to_escalate_minutes
        )
        supplement_applied = False
        if reliable_new and self.phase == "prepartum":
            base_score, model_details = score_model(features, self.model)
            score, supplement_applied = update_cooling(
                self.cooling_state, base_score, details, evaluated_at_ms, p["uid"]
            )
            model_details["score_before_supplement"] = base_score
            if supplement_applied:
                reasons.append("SUSTAINED_COOLING_ONCE_MEDIUM")
            self._advance(score, evaluated_at_ms)
        else:
            score = (
                float(self.last_output["evidence_score"]) if self.last_output else 0.0
            )
            base_score = (
                float(self.last_output["base_evidence_score"])
                if self.last_output
                else 0.0
            )
            model_details = {"kind": "carry_previous_evidence"}
            self.high_run = 0
            self.clear_run = 0
            reset_run(self.cooling_state)
        observation_fraction = float(good.mean())
        history_strength = min(1.0, len(a) / 180.0)
        weight = (
            observation_fraction
            * (0.35 + 0.65 * history_strength)
            * math.exp(-max(0, (age or 0) - 15) / 60)
            if self.last_real_time is not None
            else 0.0
        )
        if not reliable_new:
            weight = min(weight, 0.25)
        self.last_packet = dict(
            uid=p["uid"],
            source_points=len(y),
            accepted_points=int(good.sum()),
            received_at_ms=received_at_ms,
            receive_time_source=receive_time_source,
            create_time_ms=p["create_ms"],
            latest_sample_ms=p["end_ms"],
            step_seconds=p["step_ms"] / 1000,
            imu_max_dt_ms=p["imu_max_dt_ms"],
            new_real_evidence=reliable_new,
        )
        self.last_output = finite(
            dict(
                schema_version="6.0",
                algorithm_version=VERSION,
                config_sha256=self.config_hash,
                model_sha256=self.model_hash,
                cow_id=self.context.cow_id,
                device_id=self.context.device_id,
                binding_id=self.context.binding_id,
                phase=self.phase,
                evaluated_at_ms=evaluated_at_ms,
                evidence_as_of_ms=evaluated_at_ms,
                evidence_score=float(score),
                base_evidence_score=float(base_score),
                temperature_risk_level=self.level,
                new_cooling_supplement=False,
                cooling_supplement=cooling_status(self.cooling_state),
                score_meaning="temperature_evidence_not_calibrated_calving_probability",
                quality_status=source,
                fusion_weight=float(np.clip(weight, 0, 1)),
                usable_for_fusion=weight > 0,
                baseline_source=details["baseline_source"],
                latest_sample_ms=p["end_ms"],
                latest_actual_temperature_ms=self.last_real_time,
                data_age_minutes=age,
                features=features,
                feature_details=details,
                model_details=model_details,
                last_packet=self.last_packet,
                reason_codes=reasons,
                ingest_status="ACCEPTED",
                horizon_probabilities=None,
                eta_hours=None,
                fusion_policy={
                    "can_veto_other_modalities": False,
                    "standalone_urgent_alert": False,
                    "low_means_weak_temperature_evidence": True,
                },
                confirmation={
                    "high_run": self.high_run,
                    "clear_run": self.clear_run,
                    "last_positive_ms": self.last_positive,
                },
                estimated_temperature_c=float(self.filter_x[0])
                if self.filter_x is not None
                else None,
                estimation_sd_c=float(np.sqrt(self.filter_p[0, 0]))
                if self.filter_p is not None
                else None,
            )
        )
        out = self.snapshot(evaluated_at_ms)
        out["ingest_status"] = "ACCEPTED"
        out["new_cooling_supplement"] = bool(supplement_applied)
        return out

    def _legacy_features(self, end, packet_t, packet_y, packet_good):
        c = self.config
        a = np.array([[s[0], s[1]] for s in self.samples if s[2]], float).reshape(-1, 2)
        t = a[:, 0]
        y = a[:, 1]
        estimates = np.array(self.estimates, float).reshape(-1, 5)

        def obs(left, right):
            z = (t >= left) & (t <= right)
            return t[z], y[z]

        def trend_stat(left, right, q=0.5):
            z = (estimates[:, 0] >= left) & (estimates[:, 0] <= right)
            return weighted_quantile(estimates[z, 1], estimates[z, 4], q)

        current = trend_stat(end - 3 * HOUR, end)
        recent = trend_stat(end - 0.5 * HOUR, end)
        rt, ry = obs(end - 3 * HOUR, end)
        current_count = len(ry)
        same = None
        baseline = None
        source = "FIRST_PACKET"
        reference_values = np.array([])
        # Exact same-clock, then nearby previous-day observations, bounded to +/- 2 h.
        for widen in [0, 0.5, 1.0, 2.0]:
            bt, by = obs(end - (27 + widen) * HOUR, end - (24 - widen) * HOUR)
            if len(by) >= 12:
                baseline = float(np.median(by))
                reference_values = by
                source = "SAME_CLOCK" if widen == 0 else "NEAR_SAME_CLOCK"
                same = (current - baseline) if current is not None else None
                break
        if baseline is None:
            for left, right, name in [
                (12, 3, "LOCAL_3_TO_12H"),
                (6, 1, "LOCAL_1_TO_6H"),
                (3, 0.5, "LOCAL_0_5_TO_3H"),
            ]:
                bt, by = obs(end - left * HOUR, end - right * HOUR)
                if len(by) >= 12:
                    baseline = float(np.median(by))
                    reference_values = by
                    source = name
                    break
        if baseline is None and len(y) >= 12:
            n = max(5, min(20, len(y) // 3))
            reference_values = y[:n]
            baseline = float(np.median(reference_values))
            source = "FIRST_PACKET"
        comparison = current if source in ["SAME_CLOCK", "NEAR_SAME_CLOCK"] else recent
        delta = (
            comparison - baseline
            if comparison is not None and baseline is not None
            else None
        )
        # Local delta is separately defined; never masquerades as a D24 observation.
        lt, ly = obs(end - 6 * HOUR, end - HOUR)
        local_ref = float(np.median(ly)) if len(ly) >= 12 else baseline
        local_delta = (
            recent - local_ref if recent is not None and local_ref is not None else None
        )
        metrics = [m for m in self.metrics if m["end_ms"] >= end - 3 * HOUR]
        refs = [
            m for m in self.metrics if end - 12 * HOUR <= m["end_ms"] < end - 6 * HOUR
        ]
        var_source = "6_TO_12H"
        if not refs:
            refs = [
                m
                for m in self.metrics
                if end - 6 * HOUR <= m["end_ms"] < end - 3 * HOUR
            ]
            var_source = "3_TO_6H"
        if not refs:
            refs = [m for m in self.metrics if m["end_ms"] < end - 0.5 * HOUR]
            var_source = "EARLIER_PACKETS"
        pts = packet_t[packet_good]
        pys = packet_y[packet_good]
        half = len(pts) // 2
        first = metric(pts[:half], pys[:half])
        last = metric(pts[half:], pys[half:])
        if not refs and first:
            refs = [first]
            var_source = "FIRST_HALF_PACKET"

        def mid(rows, key):
            return float(np.median([m[key] for m in rows])) if rows else None

        rm = mid(metrics, "rmssd_c")
        wm = mid(metrics, "width_c")
        br = mid(refs, "rmssd_c")
        bw = mid(refs, "width_c")
        ratio = (
            rm / max(br, c.rmssd_floor_c) if rm is not None and br is not None else 1.0
        )
        wratio = (
            wm / max(bw, c.width_floor_c) if wm is not None and bw is not None else 1.0
        )
        intra = (
            last["rmssd_c"] / max(first["rmssd_c"], c.rmssd_floor_c)
            if first and last
            else 1.0
        )
        one_t, one_y = obs(end - HOUR, end)
        one = metric(one_t, one_y)
        three = metric(rt, ry)
        up_now = trend_stat(end - HOUR, end, 0.8)
        up_ref = (
            float(np.quantile(reference_values, 0.8)) if len(reference_values) else None
        )
        peak_t, peak_y = obs(end - 6 * HOUR, end - 0.25 * HOUR)
        peak = float(np.quantile(peak_y, 0.9)) if len(peak_y) >= 12 else None
        below = (
            float(np.mean(ry < baseline - 0.1))
            if len(ry) and baseline is not None
            else 0.0
        )
        trough = float(np.quantile(ry, 0.1)) if len(ry) >= 12 else None
        recovery = 0.0
        if trough is not None and recent is not None:
            trough_indices = np.flatnonzero(ry <= trough)
            if len(trough_indices) and trough_indices[0] >= 8:
                earlier_peak = float(np.quantile(ry[: trough_indices[0]], 0.9))
                if earlier_peak - trough > 0.2:
                    recovery = max(0, recent - trough)
        f = dict(
            sameclock_drop_c=max(0, -same) if same is not None else 0.0,
            local_drop_c=max(0, -local_delta) if local_delta is not None else 0.0,
            upper_envelope_drop_c=max(0, up_ref - up_now)
            if up_ref is not None and up_now is not None
            else 0.0,
            drop_from_recent_peak_c=max(0, peak - recent)
            if peak is not None and recent is not None
            else 0.0,
            downward_slope_1h_cph=max(0, -one["slope_cph"]) if one else 0.0,
            downward_slope_3h_cph=max(0, -three["slope_cph"]) if three else 0.0,
            rmssd_log2_ratio=max(0, math.log2(max(ratio, 1e-6))),
            width_log2_ratio=max(0, math.log2(max(wratio, 1e-6))),
            rmssd_absolute_c=rm or 0.0,
            width_absolute_c=wm or 0.0,
            within_packet_rmssd_log2_ratio=max(0, math.log2(max(intra, 1e-6))),
            below_reference_fraction=below,
            recovery_from_trough_c=recovery,
        )
        # Clamp implausibly extreme feature magnitudes, not temperatures in the raw archive.
        for k in f:
            limit = (
                1.0
                if k == "below_reference_fraction"
                else 0.5
                if k == "rmssd_absolute_c"
                else 4.0
            )
            f[k] = float(np.clip(f[k], 0, limit))
        detail = dict(
            baseline_source=source,
            baseline_c=baseline,
            baseline_observed_points=len(reference_values),
            current_3h_observed_points=current_count,
            current_level_3h_c=current,
            recent_level_30min_c=recent,
            delta_to_selected_baseline_c=delta,
            sameclock_delta_c=same,
            variability_reference=var_source,
            rmssd_ratio=ratio,
            width_ratio=wratio,
            recent_rmssd_c=rm,
            reference_rmssd_c=br,
            current_raw_slope_1h_cph=one["slope_cph"] if one else None,
            estimated_points_count=sum(not s[3] for s in self.estimates),
            raw_points_count=len(self.samples),
            sampling_frequency_or_history_length_used_as_positive_predictor=False,
        )
        return f, detail

    def _features(self, end, packet_t, packet_y, packet_good):
        legacy, details = self._legacy_features(end, packet_t, packet_y, packet_good)
        a = np.array([[s[0], s[1]] for s in self.samples if s[2]], float).reshape(-1, 2)
        t = a[:, 0]
        y = a[:, 1]

        def values(left, right):
            return y[(t >= left) & (t <= right)]

        source = details["baseline_source"]
        reference = np.array([])
        if source in ["SAME_CLOCK", "NEAR_SAME_CLOCK"]:
            for widen in [0, 0.5, 1.0, 2.0]:
                reference = values(end - (27 + widen) * HOUR, end - (24 - widen) * HOUR)
                if len(reference) >= 12:
                    break
        elif source.startswith("LOCAL_"):
            left, right = {
                "LOCAL_3_TO_12H": (12, 3),
                "LOCAL_1_TO_6H": (6, 1),
                "LOCAL_0_5_TO_3H": (3, 0.5),
            }[source]
            reference = values(end - left * HOUR, end - right * HOUR)
        elif len(y) >= 12:
            reference = y[: max(5, min(20, len(y) // 3))]
        noise = (
            max(
                0.15,
                float(1.4826 * np.median(np.abs(reference - np.median(reference)))),
            )
            if len(reference)
            else 0.15
        )
        delta = details["delta_to_selected_baseline_c"]
        drop = max(0, -delta) if delta is not None else 0.0
        features = dict(
            B_selected_reference_drop=float(np.clip(drop / noise, 0, 3)),
            B_relative_rmssd_gain=float(
                np.clip(math.log2(max(details["rmssd_ratio"], 1e-6)), 0, 3)
            ),
            B_relative_width_gain=float(
                np.clip(math.log2(max(details["width_ratio"], 1e-6)), 0, 3)
            ),
            B_absolute_variability_support=float(
                np.clip(
                    min(
                        legacy["rmssd_absolute_c"] / 0.08,
                        legacy["width_absolute_c"] / 0.4,
                    ),
                    0,
                    2,
                )
            ),
        )
        details.update(
            reference_noise_c=noise,
            reference_observed_points=len(reference),
            selected_reference_drop_c=drop,
            selected_drop_reference_source=source,
            legacy_features_for_trace_only=legacy,
            actual_scoring_features=FEATURE_NAMES,
            supplement_scoring_features=[
                "delta_to_selected_baseline_c",
                "upper_envelope_drop_c",
                "below_reference_fraction",
            ],
        )
        return features, details

    def _advance(self, score, now):
        c = self.config
        if (
            self.last_real_decision is not None
            and now - self.last_real_decision
            > c.maximum_confirmation_gap_minutes * MINUTE
        ):
            self.high_run = 0
            self.clear_run = 0
            self.level = "LOW"
            self.last_positive = None
        self.last_real_decision = now
        if score >= c.medium_score:
            self.last_positive = now
            self.clear_run = 0
            self.high_run = self.high_run + 1 if score >= c.high_score else 0
            if self.high_run >= c.high_confirmations:
                self.level = "HIGH"
            elif self.level != "HIGH":
                self.level = "MEDIUM"
            elif score < c.high_score:
                self.level = "MEDIUM"
        else:
            self.high_run = 0
            self.clear_run = (
                self.clear_run + 1 if score < c.medium_score - c.clear_margin else 0
            )
            if self.clear_run >= c.clear_confirmations and (
                self.last_positive is None
                or now - self.last_positive >= c.hold_minutes * MINUTE
            ):
                self.level = "LOW"

    def snapshot(self, now_ms):
        self._clock(now_ms)
        self.last_clock = now_ms
        if self.last_output is None:
            return dict(
                schema_version="6.0",
                algorithm_version=VERSION,
                cow_id=self.context.cow_id,
                device_id=self.context.device_id,
                binding_id=self.context.binding_id,
                evaluated_at_ms=now_ms,
                evidence_score=0.0,
                base_evidence_score=0.0,
                new_cooling_supplement=False,
                cooling_supplement=cooling_status(self.cooling_state),
                temperature_risk_level="LOW",
                quality_status="INITIAL_PRIOR"
                if self.phase == "prepartum"
                else "NOT_APPLICABLE",
                score_meaning="temperature_evidence_not_calibrated_calving_probability",
                fusion_weight=0.0,
                usable_for_fusion=False,
                phase=self.phase,
                reason_codes=["NO_OBSERVATION_YET_INITIAL_EVIDENCE"],
                ingest_status="SNAPSHOT",
                features={},
                eta_hours=None,
                horizon_probabilities=None,
            )
        out = json.loads(json.dumps(self.last_output))
        out["evaluated_at_ms"] = now_ms
        out["ingest_status"] = "SNAPSHOT"
        out["phase"] = self.phase
        if self.filter_x is not None:
            x, p = predict_filter(
                self.filter_x,
                self.filter_p,
                max(0, (now_ms - self.filter_time) / MINUTE),
                self.config,
            )
            out["estimated_temperature_c"] = self._gap_estimate(x[0], now_ms)
            out["estimation_sd_c"] = float(np.sqrt(p[0, 0]))
            out["data_age_minutes"] = (now_ms - self.last_real_time) / MINUTE
        elapsed = (now_ms - out["evidence_as_of_ms"]) / MINUTE
        if elapsed > 0:
            out["quality_status"] = "FORECAST_CONTINUES"
            out["fusion_weight"] *= math.exp(-elapsed / 90.0)
            out["reason_codes"] = list(
                dict.fromkeys(
                    out["reason_codes"] + ["NO_NEW_REAL_EVIDENCE_GRADE_NOT_PROMOTED"]
                )
            )
        if self.phase != "prepartum":
            out.update(
                evidence_score=0.0,
                base_evidence_score=0.0,
                temperature_risk_level="LOW",
                quality_status="NOT_APPLICABLE",
                fusion_weight=0.0,
            )
        out["new_cooling_supplement"] = False
        out["cooling_supplement"] = cooling_status(self.cooling_state)
        out["usable_for_fusion"] = out["fusion_weight"] > 1e-6
        return finite(out)

    def set_phase(self, phase, at_ms):
        if phase not in ["prepartum", "calving", "postpartum", "inactive"]:
            raise ValueError("invalid phase")
        self._clock(at_ms)
        if phase != self.phase:
            if phase == "prepartum":
                raise ValueError(
                    "create a fresh binding/module for a new prepartum episode"
                )
            self.phase = phase
            reset_run(self.cooling_state)
        return self.snapshot(at_ms)

    def to_state_json(self):
        state = {
            k: v.tolist() if isinstance(v, np.ndarray) else v
            for k, v in self.__dict__.items()
            if k not in ["context", "config", "model"]
        }
        return json.dumps(
            finite(
                dict(
                    state_schema=6,
                    algorithm_version=VERSION,
                    context=asdict(self.context),
                    config=asdict(self.config),
                    model=self.model,
                    state=state,
                )
            ),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_state_json(cls, text):
        d = json.loads(text)
        if d.get("state_schema") != 6 or d.get("algorithm_version") != VERSION:
            raise ValueError("state version mismatch")
        m = cls(Context(**d["context"]), Config(**d["config"]), d["model"])
        allowed = set(m.__dict__) - {"context", "config", "model"}
        if set(d["state"]) != allowed:
            raise ValueError("invalid state fields")
        m.__dict__.update(d["state"])
        for k in ["filter_x", "filter_p"]:
            if m.__dict__[k] is not None:
                m.__dict__[k] = np.array(m.__dict__[k], float)
        return m


def as_fusion_features(output, *, now_ms):
    if now_ms < output["evaluated_at_ms"]:
        raise ValueError("fusion time precedes prediction")
    age = (now_ms - output["evaluated_at_ms"]) / MINUTE
    return dict(
        temperature_evidence_score=output["evidence_score"],
        temperature_evidence_grade=output["temperature_risk_level"],
        temperature_evidence_weight=output.get("fusion_weight", 0)
        * math.exp(-age / 90.0),
        temperature_estimated=output["quality_status"] != "REAL_UPDATED"
        or now_ms > output["evaluated_at_ms"],
        can_veto_other_modalities=False,
        standalone_urgent_alert=False,
    )
