"""Pure behavior-algorithm catalog and external artifact contract.

Research folders are inputs to this catalog, never runtime imports. The
runtime loads only versioned model suites and writes generated material to
the configured model home.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from . import EVENT_TITLES
from .paths import ensure_runtime_layout


@dataclass(frozen=True)
class BehaviorAlgorithmSpec:
    code: str
    title: str
    research_folder: str
    implementation: str
    train_entry: str
    predict_entry: str
    candidate_entry: str
    external_model_dir: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


SPECS = (
    BehaviorAlgorithmSpec("STANDING_UP", EVENT_TITLES["STANDING_UP"], "standup_algo",
                         "cowmata_tailring.algorithms.behavior.standup.detector", "cowmata_tailring.algorithms.behavior.standup.train",
                         "cowmata_tailring.algorithms.behavior.standup.detector", "cowmata_tailring.algorithms.behavior.standup.detector", "STANDING_UP"),
    BehaviorAlgorithmSpec("LYING_DOWN", EVENT_TITLES["LYING_DOWN"], "liedown_study",
                         "cowmata_tailring.algorithms.liedown", "cowmata_tailring.algorithms.liedown_train",
                         "cowmata_tailring.algorithms.liedown", "cowmata_tailring.algorithms.liedown", "LYING_DOWN"),
    BehaviorAlgorithmSpec("STRAINING_BOUT", EVENT_TITLES["STRAINING_BOUT"], "straining_lab",
                         "cowmata_tailring.algorithms.straining",
                         "cowmata_tailring.algorithms.straining.train",
                         "cowmata_tailring.algorithms.straining", "cowmata_tailring.algorithms.straining", "STRAINING_BOUT"),
    BehaviorAlgorithmSpec("URINATION", EVENT_TITLES["URINATION"], "urination_algo",
                         "cowmata_tailring.algorithms.behavior.urination.detector", "cowmata_tailring.algorithms.behavior.urination.train",
                         "cowmata_tailring.algorithms.behavior.urination.detector", "cowmata_tailring.algorithms.behavior.urination.detector", "URINATION"),
    BehaviorAlgorithmSpec("FETAL_PART_FIRST_VISIBLE", EVENT_TITLES["FETAL_PART_FIRST_VISIBLE"],
                         "fetal_part_first_visible", "cowmata_tailring.algorithms.fpfv",
                         "cowmata_tailring.algorithms.behavior.fpfv_training",
                         "cowmata_tailring.algorithms.fpfv", "cowmata_tailring.algorithms.fpfv", "FPFV"),
    BehaviorAlgorithmSpec("CALF_FULLY_EXPELLED", EVENT_TITLES["CALF_FULLY_EXPELLED"],
                         "calf_expelled", "cowmata_tailring.algorithms.behavior.calf_expelled",
                         "cowmata_tailring.algorithms.behavior.calf_expelled", "cowmata_tailring.algorithms.behavior.calf_expelled",
                         "cowmata_tailring.algorithms.behavior.calf_expelled", "CALF_FULLY_EXPELLED"),
)


def catalog() -> tuple[BehaviorAlgorithmSpec, ...]:
    return SPECS


def spec_for(code: str) -> BehaviorAlgorithmSpec:
    for spec in SPECS:
        if spec.code == code:
            return spec
    raise KeyError(f"Unsupported behavior code: {code}")


def artifact_dir(code: str, *, root: Path | None = None) -> Path:
    home = ensure_runtime_layout(root)
    return home / "experiments" / spec_for(code).external_model_dir


def training_dir(code: str, *, root: Path | None = None) -> Path:
    path = artifact_dir(code, root=root) / "training"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prediction_dir(code: str, *, root: Path | None = None) -> Path:
    path = artifact_dir(code, root=root) / "predictions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def capability_matrix(*, root: Path | None = None) -> list[dict[str, object]]:
    home = ensure_runtime_layout(root)
    rows = []
    for spec in SPECS:
        path = artifact_dir(spec.code, root=home)
        rows.append({
            **spec.to_dict(),
            "model_home": str(home),
            "artifact_dir": str(path),
            "has_versioned_models": any(path.glob("**/suite.json")) or any(path.glob("**/*model*.json")),
            "outputs_are_external": True,
        })
    return rows
