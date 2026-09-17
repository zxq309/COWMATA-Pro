"""Reconstruct posture only inside observed, nonconflicting transition spans."""
from __future__ import annotations


def occupancy(events, start_ms, end_ms, *, observed_intervals, max_hold_ms=3600000):
    if end_ms <= start_ms:
        raise ValueError('Posture window must have positive duration')
    totals = dict(lying_seconds=0., standing_seconds=0., transition_seconds=0., unknown_seconds=0.)
    conflicts = 0
    segments = []
    for a, b in sorted(observed_intervals):
        if b <= a:
            continue
        if segments and a <= segments[-1][1]:
            segments[-1][1] = max(b, segments[-1][1])
        else:
            segments.append([a, b])
    all_events = sorted((e for e in events if e.get('code') in {'STANDING_UP', 'LYING_DOWN'}), key=lambda e: e['start_ms'])
    for a, b in segments:
        ranges = []
        cursor, state, last_end = a, 'unknown', None
        for event in all_events:
            left = event['start_ms']
            r = event.get('end_ms')
            # Approximate legacy points cannot imply exact posture boundaries.
            if r is None or r <= left or left < a or r > b:
                continue
            next_state = 'standing' if event['code'] == 'STANDING_UP' else 'lying'
            if left < cursor:
                conflicts += 1
                if ranges:
                    ranges[-1][1] = max(ranges[-1][1], r)
                    ranges[-1][2] = 'unknown'
                state = 'unknown'
                last_end = None
                cursor = max(cursor, r)
                continue
            conflict = state == next_state
            if conflict:
                conflicts += 1
            label = 'unknown' if conflict else state
            stop = min(left, last_end+max_hold_ms) if last_end is not None else left
            if stop > cursor:
                ranges.append([cursor, stop, label])
            if left > stop:
                ranges.append([stop, left, 'unknown'])
            ranges.append([left, r, 'transition'])
            cursor, state, last_end = r, next_state, r
        stop = min(b, last_end+max_hold_ms) if last_end is not None else b
        if stop > cursor:
            ranges.append([cursor, stop, state])
        if b > stop:
            ranges.append([stop, b, 'unknown'])
        for left, r, label in ranges:
            totals[label+'_seconds'] += max(0., min(end_ms, r)-max(start_ms, left))/1000
    observed = sum(max(0., min(end_ms, b)-max(start_ms, a)) for a, b in segments)/1000
    duration = (end_ms-start_ms)/1000
    known = totals['lying_seconds']+totals['standing_seconds']
    missing = max(0., duration-observed)
    uncertain = totals['unknown_seconds']+missing
    return dict(**totals, missing_seconds=missing, observed_seconds=observed, conflicts=conflicts,
        lying_fraction_known=totals['lying_seconds']/known if known else None,
        known_posture_coverage=known/observed if observed else 0.,
        lying_fraction_lower=totals['lying_seconds']/duration,
        lying_fraction_upper=min(1., (totals['lying_seconds']+uncertain)/duration),
        interpretation='transition_inferred_not_direct_posture_measurement')
