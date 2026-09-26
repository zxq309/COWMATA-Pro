"""Registry of the seven calving decision features.

Each module is owned by one "统计数据规律与决策预测" session; the decision engine only
talks to them through :mod:`cowmata_engine.features.base`. Missing or broken modules are
reported, never silently replaced by zeros.
"""
from __future__ import annotations

import importlib

from .base import FEATURE_API, WINDOW_MS, FeatureSpec, validate_rows

# key -> (module, owner session title)
FEATURE_MODULES = {
    "heart_rate": ("cowmata_engine.features.heart_rate", "心率"),
    "spo2": ("cowmata_engine.features.spo2", "血氧"),
    "activity": ("cowmata_engine.features.activity", "活动量"),
    "temperature": ("cowmata_engine.features.temperature", "温度"),
    "lying_ratio": ("cowmata_engine.features.lying_ratio", "躺卧占比"),
    "straining_ratio": ("cowmata_engine.features.straining_ratio", "努责占比"),
    "gyro_spectral_entropy": ("cowmata_engine.features.gyro_spectral_entropy", "角速度频谱熵"),
}


def load_feature(key):
    module_name, _ = FEATURE_MODULES[key]
    module = importlib.import_module(module_name)
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, FeatureSpec) or spec.key != key:
        raise ValueError(f"{key}: 模块未按 {FEATURE_API} 提供 SPEC")
    if not callable(getattr(module, "extract", None)) and not callable(getattr(module, "extract_series", None)):
        raise ValueError(f"{key}: 模块缺少 extract/extract_series")
    return module


def available_features():
    """[(key, module | None, error | None)] in the canonical order."""
    result = []
    for key in FEATURE_MODULES:
        try:
            result.append((key, load_feature(key), None))
        except (ImportError, ValueError, AttributeError) as exc:
            result.append((key, None, str(exc)))
    return result


def feature_catalog():
    """JSON-serialisable status for UI / external front-end."""
    rows = []
    for key, module, error in available_features():
        title = FEATURE_MODULES[key][1]
        if module is None:
            rows.append(dict(key=key, title=title, ready=False, error=error))
        else:
            rows.append(dict(ready=True, error=None, series=hasattr(module, "extract_series"),
                             **module.SPEC.as_dict()))
    return rows


__all__ = ["FEATURE_API", "WINDOW_MS", "FEATURE_MODULES", "FeatureSpec", "available_features",
           "feature_catalog", "load_feature", "validate_rows"]
