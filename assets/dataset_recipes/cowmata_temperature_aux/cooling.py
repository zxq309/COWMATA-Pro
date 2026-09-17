"""Frozen cool05_run2_once rule; inputs contain no animal identity or labels.

Only call update_cooling for an accepted packet with fresh real evidence.
All state is JSON serializable so restart preserves the once-per-run latch.
"""

MINUTE = 60000
RULE_ID = "cool05_run2_once"


def new_state():
    return dict(
        run=0,
        start_ms=None,
        last_ms=None,
        emitted=False,
        qualified=False,
        span_minutes=0.0,
        last_trigger_ms=None,
        last_trigger_packet_uid=None,
        last_score_lifted_ms=None,
    )


def reset_run(state):
    """Break confirmation, retaining the historical trigger for audit."""
    state.update(run=0, start_ms=None, emitted=False, qualified=False, span_minutes=0.0)


def update_cooling(state, base_score, details, now_ms, packet_uid):
    if state["last_ms"] is not None and now_ms - state["last_ms"] > 90 * MINUTE:
        reset_run(state)
    state["last_ms"] = now_ms
    legacy = details.get("legacy_features_for_trace_only", {})
    delta = details.get("delta_to_selected_baseline_c")
    qualified = (
        details.get("baseline_source") == "SAME_CLOCK"
        and details.get("reference_observed_points", 0) >= 60
        and details.get("current_3h_observed_points", 0) >= 60
        and delta is not None
        and delta <= -0.5
        and legacy.get("upper_envelope_drop_c", 0) >= 0.5
        and legacy.get("below_reference_fraction", 0) >= 0.8
    )
    if qualified:
        if state["run"] == 0:
            state["start_ms"] = now_ms
        state["run"] += 1
    else:
        reset_run(state)
    state["qualified"] = bool(qualified)
    state["span_minutes"] = (
        (now_ms - state["start_ms"]) / MINUTE if state["start_ms"] is not None else 0.0
    )
    trigger = state["run"] >= 2 and state["span_minutes"] >= 60 and not state["emitted"]
    if trigger:
        # Consume even when B already supports MEDIUM/HIGH: no second lift in
        # the same uninterrupted qualifying run if B later weakens.
        state["emitted"] = True
        state["last_trigger_ms"] = now_ms
        state["last_trigger_packet_uid"] = str(packet_uid)
    score = max(float(base_score), 70.0) if trigger else float(base_score)
    applied = score > base_score
    if applied:
        state["last_score_lifted_ms"] = now_ms
    return score, applied


def cooling_status(state):
    return dict(
        rule_id=RULE_ID,
        qualified=state["qualified"],
        consecutive_packets=state["run"],
        span_minutes=state["span_minutes"],
        episode_consumed=state["emitted"],
        last_trigger_ms=state["last_trigger_ms"],
        last_trigger_packet_uid=state["last_trigger_packet_uid"],
        last_score_lifted_ms=state["last_score_lifted_ms"],
    )
