"""Inference-only decision runtime shared by the client and exported predictor."""
from __future__ import annotations

import numpy as np

HOUR_MS = 3_600_000
MANIFEST_SCHEMA = "cowmata-decision-4.3.8"


def hval(h):
    """A prediction boundary in hours as a number: 12 / 2.5 (accepts 12, "12", "12h", 2.5, "2.5h")."""
    value = float(str(h).strip().rstrip("hH"))
    return int(value) if value.is_integer() else value


def hkey(h):
    """Canonical text key of a boundary: "12", "2.5" (manifest keys, column names ``risk_<key>h``)."""
    return f"{hval(h):g}"


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
