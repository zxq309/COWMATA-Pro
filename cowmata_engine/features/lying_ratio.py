"""躺卧占比 (lying ratio) decision feature, plug-in ``cowmata-decision-feature-1``.

Share of observed time in each absolute 10-min window that the cow is lying, from the tail-ring
9-axis signal with :mod:`cowmata_tailring.algorithms.lying_occupancy` (``lying-occupancy-2``):
per-second posture probability fused with the stand-up (起立过程) and lie-down (卧倒过程)
transition detectors in a two-state hidden Markov model.  Unlike the 4.3.2 transition-only
reconstruction, every observed second gets a posture, so the ratio is defined for the whole day.

Validation (10-fold grouped by cow, video-labelled posture truth, 27 cows, 155 k s; see
``躺卧占比/evaluation/lying_ratio_validation.json``): per-second accuracy 0.986; 10-min lying
ratio mean absolute error 1.7 percentage points (96 % of windows within ±5 pp); within ±8 h of
calf expulsion 0.943 / 6.8 pp.  Lie-down recall 0.96 and stand-up recall 0.92 (±60 s).
The 4.3.2 transition-only method, even when fed the labelled transitions, had 22.5 pp error.

Model files (never stored in the source tree): a folder with ``posture.json``,
``transition.json``, ``hmm.json`` and ``lying_ratio_params.json`` —
``$COWMATA_LYING_RATIO_MODEL`` or ``<model_home>/experiments/LYING_RATIO/lying_ratio``.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from cowmata_engine.features.base import WINDOW_MS, FeatureSpec, finite_or_none

PARAMS_SCHEMA = "cowmata-lying-ratio-params-1"
PARAMS_FILE = "lying_ratio_params.json"
MODEL_FILES = ("posture.json", "transition.json", "hmm.json")

from cowmata_tailring.algorithms.lying_occupancy import (  # noqa: E402
    ALGORITHM,
    LOOKAHEAD_MS,
    ROW_COLUMNS,
)

SPEC = FeatureSpec(
    key="lying_ratio",
    title="躺卧占比",
    modality="motion",
    version=ALGORITHM,
    columns=ROW_COLUMNS,
    primary="lying_ratio",
    unit="fraction",
    lookahead_ms=LOOKAHEAD_MS,
    expected_change=("产前 3 天内躺卧占比按本牛日节律波动（全天均值约 0.5）；娩出前 24 h 躺卧总时长减少、"
                     "起卧转换增多、单次躺卧缩短；娩出前 6~2 h 躺卧/站立交替最频繁（起卧次数约为本牛基线 2 倍），"
                     "第二产程多为躺卧并伴抬尾努责；娩出后 1 h 内站立舔犊，躺卧占比骤降。"
                     "详见 躺卧占比/report/躺卧占比_研究报告.md。"),
    column_titles={
        "lying_ratio": "躺卧占比（HMM 后验均值）",
        "lying_ratio_hard": "躺卧占比（状态路径）",
        "lying_confidence": "姿态判定置信度（|2q−1| 均值）",
        "lying_down_count": "卧倒次数（窗口内开始的躺卧段）",
        "standing_up_count": "起立次数（窗口内结束的躺卧段）",
        "lying_bout_minutes": "窗口内结束的完整躺卧段平均时长（分钟）",
        "standing_reference_ok": "站立参考方向已由本牛活动估计的比例",
    },
)


def default_model_dir() -> Path:
    configured = os.environ.get("COWMATA_LYING_RATIO_MODEL", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    from cowmata_tailring.algorithms.paths import model_home

    return model_home() / "experiments" / "LYING_RATIO" / "lying_ratio"


@lru_cache(maxsize=4)
def _load(folder: str):
    folder = Path(folder)
    params = json.loads((folder / PARAMS_FILE).read_text(encoding="utf-8"))
    if params.get("schema") != PARAMS_SCHEMA or params.get("algorithm") != ALGORITHM:
        raise ValueError("不是躺卧占比 lying-occupancy-2 参数文件")
    models = {}
    for name in MODEL_FILES:
        data = (folder / name).read_bytes()
        expected = (params.get("sha256") or {}).get(name)
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"躺卧占比模型文件校验失败：{name}")
        models[name.split(".")[0]] = json.loads(data.decode("utf-8"))
    return models


def load_model(folder=None):
    folder = Path(folder) if folder else default_model_dir()
    missing = [n for n in (*MODEL_FILES, PARAMS_FILE) if not (folder / n).is_file()]
    if missing:
        raise FileNotFoundError(f"缺少躺卧占比模型：{folder}（需要 {', '.join(missing)}）")
    return _load(str(folder.resolve()))


def _rows(records, sources, models, window_ms):
    from cowmata_tailring.algorithms.lying_occupancy import analyse_series

    rows, detail = analyse_series(records, models, window_ms=window_ms)
    starts = sorted((e, src) for (e, _), src in zip(records, sources))
    out = []
    for row in rows:
        owner = sources[0]
        for e, src in starts:
            if e <= row["start_epoch_ms"] + window_ms:
                owner = src
        clean = {k: (finite_or_none(v) if k in SPEC.columns else v) for k, v in row.items()}
        clean["source"] = owner
        out.append(clean)
    return out


def extract_series(sources, *, window_ms=WINDOW_MS, model_dir=None):
    """Rows for one cow/device; consecutive files keep posture state across packets."""
    from cowmata_tailring.algorithms.lying_occupancy import summarize_motion_file

    models = load_model(model_dir)
    records, kept = [], []
    for path in sources:
        epoch0, summary, _ = summarize_motion_file(path)
        if len(summary["coverage"]):
            records.append((epoch0, summary))
            kept.append(str(path))
    if not records:
        return []
    return _rows(records, kept, models, window_ms)


def extract(source, *, window_ms=WINDOW_MS, model_dir=None):
    return extract_series([source], window_ms=window_ms, model_dir=model_dir)


def extract_summaries(records, sources, *, window_ms=WINDOW_MS, model_dir=None):
    """Same as :func:`extract_series` from precomputed per-second summaries (epoch0_ms, summary)."""
    if not records:
        return []
    return _rows(list(records), [str(s) for s in sources], load_model(model_dir), window_ms)
