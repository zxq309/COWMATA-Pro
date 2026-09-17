from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MODEL_FORMAT_VERSION = 1
PREPROCESSING_VERSION = "imu13-v3"
BODY_CLASSES = ("FEEDING", "LYING", "STANDING", "WALKING")
EVENT_CLASSES = (
    "DEFECATION",
    "URINATION",
    "LYING_DOWN",
    "STANDING_UP",
    "TAIL_RAISED",
    "TAIL_WAGGING",
)

LABEL_ZH = {
    "FEEDING": "采食",
    "LYING": "躺卧",
    "STANDING": "站立",
    "WALKING": "行走",
    "DEFECATION": "排便",
    "URINATION": "排尿",
    "LYING_DOWN": "卧倒",
    "STANDING_UP": "起立",
    "TAIL_RAISED": "抬尾",
    "TAIL_WAGGING": "甩尾",
}

LABEL_LAYER = {
    **{code: "body_state" for code in BODY_CLASSES},
    "DEFECATION": "objective_event",
    "URINATION": "objective_event",
    "LYING_DOWN": "posture_transition",
    "STANDING_UP": "posture_transition",
    "TAIL_RAISED": "tail_action",
    "TAIL_WAGGING": "tail_action",
}

FEATURE_NAMES = (
    "axis_0",
    "axis_1",
    "axis_2",
    "axis_3",
    "axis_4",
    "axis_5",
    "axis_6",
    "axis_7",
    "axis_8",
    "triad_0_norm",
    "triad_1_norm",
    "triad_2_norm",
    "gap_mask",
)


@dataclass(frozen=True)
class SessionRecord:
    imu_path: Path
    annotation_path: Path | None = None
    split: str = "unlabelled"
    session_id: str = ""
    device_id: str = ""
    cow_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.device_id}/{self.session_id}"


@dataclass
class ModelConfig:
    input_channels: int = len(FEATURE_NAMES)
    body_classes: int = len(BODY_CLASSES)
    event_classes: int = len(EVENT_CLASSES)
    width: int = 96
    dilations: tuple[int, ...] = (1, 2, 4, 8, 16, 32)
    refinement_dilations: tuple[int, ...] = (1, 2, 4, 8)
    dropout: float = 0.15
    sample_hz: float = 50.0
    output_stride: int = 25
    chunk_seconds: float = 60.0
    chunk_overlap: float = 0.5

    @property
    def chunk_samples(self) -> int:
        raw = int(round(self.chunk_seconds * self.sample_hz))
        return max(self.output_stride, raw // self.output_stride * self.output_stride)

    @property
    def output_hz(self) -> float:
        return self.sample_hz / self.output_stride

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["dilations"] = list(self.dilations)
        result["refinement_dilations"] = list(self.refinement_dilations)
        return result

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> ModelConfig:
        allowed = set(cls.__dataclass_fields__)
        clean = {key: value for key, value in values.items() if key in allowed}
        if "dilations" in clean:
            clean["dilations"] = tuple(clean["dilations"])
        if "refinement_dilations" in clean:
            clean["refinement_dilations"] = tuple(clean["refinement_dilations"])
        return cls(**clean)


@dataclass
class NormalizationStats:
    center: list[float] = field(default_factory=list)
    scale: list[float] = field(default_factory=list)
    method: str = "median_mad"
    fitted_split: str = "train_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> NormalizationStats:
        return cls(**values)
