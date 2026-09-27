"""Data-driven straining regularity ("规律") report, recomputed whenever labels grow."""

from __future__ import annotations

import numpy as np

from . import signal

CONTEXT_CODES = ("LYING_TAIL_WAGGING", "STANDING_TAIL_WAGGING", "URINATION", "DEFECATION")


def _pct(values, qs=(10, 25, 50, 75, 90)):
    values = np.asarray([v for v in values if np.isfinite(v)], float)
    return {f"p{q}": round(float(np.percentile(values, q)), 3) for q in qs} if len(values) else None


def bout_pulse_stats(feature, events):
    P = feature["pulses"]
    q = P["quiet"].astype(bool)
    out = []
    for e in events:
        a, b = e["start_ms"] / 1000, e["end_ms"] / 1000
        inside = (P["t"] >= a) & (P["t"] <= b)
        qt = np.sort(P["t"][inside & q])
        ipi = np.diff(qt)
        v = np.column_stack([P["dx"][inside & q], P["dy"][inside & q], P["dz"][inside & q]])
        out.append(
            dict(
                duration_s=b - a,
                quiet_pulses=int(len(qt)),
                loud_pulses=int((inside & ~q).sum()),
                pulses_per_min=len(qt) / max(b - a, 1) * 60,
                ipi_median_s=float(np.median(ipi)) if len(ipi) else np.nan,
                prominence_deg=float(np.median(P["prom"][inside & q])) if len(qt) else np.nan,
                width_s=float(np.median(P["width"][inside & q])) if len(qt) else np.nan,
                pulse_hf_dps=float(np.median(P["hf"][inside])) if inside.any() else np.nan,
                direction_R=float(np.linalg.norm(v.sum(0)) / len(v)) if len(v) >= 3 else np.nan,
            )
        )
    return out


def regularity_report(items):
    """items: list of dict(cow_id, feature, straining=[events], context={code: [events]})."""
    bouts, per_cow, ctx = [], {}, {}
    for it in items:
        stats = bout_pulse_stats(it["feature"], it["straining"])
        for s in stats:
            s["cow_id"] = it["cow_id"]
        bouts += stats
        for code, evs in it.get("context", {}).items():
            ctx.setdefault(code, []).extend(
                bout_pulse_stats(it["feature"], [e for e in evs if e.get("end_ms")])
            )
    if not bouts:
        return dict(bouts=0)
    for b in bouts:
        per_cow.setdefault(b["cow_id"], []).append(b)

    def rule(b):
        return b["quiet_pulses"] >= 2 and (
            not np.isfinite(b["ipi_median_s"]) or 1.5 <= b["ipi_median_s"] <= 9
        )

    cow_rate = {c: float(np.median([x["pulses_per_min"] for x in v])) for c, v in per_cow.items()}
    report = dict(
        feature_version=signal.FEATURE_VERSION,
        bouts=len(bouts),
        cows=len(per_cow),
        law=(
            "努责=安静的尾部倾角脉冲串：每次腹部用力使尾根产生约1 s、数度的平滑倾角脉冲，"
            "脉冲按约2.5–5.5 s间隔重复（每分钟约10–22次）；脉冲期间高频陀螺能量低（区别于甩尾的强烈抖动）。"
        ),
        conformance=dict(
            share_bouts_ge1_quiet_pulse=round(
                float(np.mean([b["quiet_pulses"] >= 1 for b in bouts])), 4
            ),
            share_bouts_ge2_quiet_pulses=round(
                float(np.mean([b["quiet_pulses"] >= 2 for b in bouts])), 4
            ),
            share_bouts_rule=round(float(np.mean([rule(b) for b in bouts])), 4),
            share_cows_median_rate_6_30_per_min=round(
                float(np.mean([6 <= r <= 30 for r in cow_rate.values()])), 4
            ),
        ),
        straining=dict(
            (k, _pct([b[k] for b in bouts]))
            for k in (
                "duration_s",
                "quiet_pulses",
                "pulses_per_min",
                "ipi_median_s",
                "prominence_deg",
                "width_s",
                "pulse_hf_dps",
                "direction_R",
                "loud_pulses",
            )
        ),
        per_cow_pulses_per_min={c: round(r, 2) for c, r in sorted(cow_rate.items())},
        contrast={
            code: dict(
                n=len(v),
                pulse_hf_dps=_pct([b["pulse_hf_dps"] for b in v]),
                prominence_deg=_pct([b["prominence_deg"] for b in v]),
                pulses_per_min=_pct([b["pulses_per_min"] for b in v]),
            )
            for code, v in ctx.items()
            if v
        },
        quiet_hf_threshold_dps=signal.QUIET_HF_DPS,
    )
    return report


def drift(previous, current):
    """Compare medians of the core regularity between two reports (for incremental data)."""
    if not previous or "straining" not in previous:
        return None
    rows = []
    for key in (
        "duration_s",
        "pulses_per_min",
        "ipi_median_s",
        "prominence_deg",
        "width_s",
        "pulse_hf_dps",
    ):
        a, b = (previous["straining"].get(key) or {}), (current["straining"].get(key) or {})
        if a and b:
            rows.append(
                dict(
                    metric=key,
                    previous_p50=a["p50"],
                    current_p50=b["p50"],
                    change_pct=round(100 * (b["p50"] - a["p50"]) / max(abs(a["p50"]), 1e-9), 1),
                )
            )
    rows.append(
        dict(
            metric="share_bouts_rule",
            previous_p50=previous["conformance"]["share_bouts_rule"],
            current_p50=current["conformance"]["share_bouts_rule"],
            change_pct=None,
        )
    )
    return rows
