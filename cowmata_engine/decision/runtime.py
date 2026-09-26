"""Inference-only decision runtime shared by the client and exported predictor."""
from __future__ import annotations

import numpy as np

HOUR_MS = 3_600_000
MANIFEST_SCHEMA = "cowmata-decision-4.3.4"


def alert_episodes(times, prob, threshold, *, persistence=2, gap_ms=HOUR_MS):
    """Return sustained alert episodes using a model-provided threshold."""
    threshold = float(threshold)
    if not 0.0 < threshold < 1.0:
        raise ValueError("model threshold must be in (0, 1)")
    persistence = max(1, int(persistence))
    episodes, run = [], []
    for t, p in zip(times, prob):
        if np.isfinite(p) and p >= threshold and (not run or t - run[-1][0] <= gap_ms):
            run.append((t, p))
            continue
        if len(run) >= persistence:
            episodes.append(dict(start=run[persistence - 1][0], first=run[0][0], end=run[-1][0],
                                 peak=max(v for _, v in run), windows=len(run)))
        run = [(t, p)] if np.isfinite(p) and p >= threshold else []
    if len(run) >= persistence:
        episodes.append(dict(start=run[persistence - 1][0], first=run[0][0], end=run[-1][0],
                             peak=max(v for _, v in run), windows=len(run)))
    return episodes
