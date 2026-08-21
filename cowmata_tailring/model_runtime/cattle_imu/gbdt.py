"""Serialization-compatible runtime subset of the 20260816 GBDT backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class BoosterConfig:
    n_estimators: int = 400
    learning_rate: float = 0.05
    max_depth: int = 6
    subsample: float = 0.8
    colsample: float = 0.8
    min_child_weight: float = 5.0
    reg_lambda: float = 1.0
    device: str = "cuda"
    random_state: int = 20260815
    extra: dict[str, Any] = field(default_factory=dict)


class BinaryBooster:
    """Class path and prediction method used by ``gbdt_full.joblib``."""

    def __init__(
        self,
        config: BoosterConfig | None = None,
        backend: str | None = None,
    ) -> None:
        self.config = config or BoosterConfig()
        self.backend = backend or "xgboost"
        self.model: Any = None
        self.n_features: int | None = None

    def predict_proba(self, values: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("model is not fitted")
        probabilities = self.model.predict_proba(
            np.asarray(values, dtype=np.float32)
        )
        return np.asarray(probabilities, dtype=np.float64)[:, 1]
