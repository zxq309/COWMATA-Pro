"""Application-layer routing for algorithm UI windows.

Qt windows use these small functions instead of importing model storage,
registry, or worker internals directly. The functions keep the result and
model roots outside the shipped source tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from cowmata_tailring.algorithms.behavior_dispatch import predict_behavior
from cowmata_tailring.algorithms.candidate_service import run_candidate_scan
from cowmata_tailring.algorithms.paths import (
    algorithm_output_root,
    config_path,
    dataset_root,
    ensure_runtime_layout,
    model_home,
    read_settings,
    save_settings,
)
from cowmata_tailring.algorithms.registry import (
    activate,
    active_suite,
    default_home,
    install_suite,
    list_suites,
)
from cowmata_tailring.algorithms.runner import run_job
from cowmata_tailring.workspace.event_models import available_packs


def runtime_roots() -> dict[str, Path | None]:
    """Return configured roots for display and service routing."""
    return {
        "model_home": model_home(),
        "dataset_home": dataset_root(),
        "output_root": algorithm_output_root(),
    }


def ensure_model_layout() -> Path:
    return ensure_runtime_layout(model_home())


def job(request: dict[str, Any], *, cancelled: Callable[[], bool] = lambda: False, progress_path: Path | None = None) -> Any:
    """Run one bounded numerical job from a UI request."""
    return run_job(request, cancelled=cancelled, progress_path=progress_path)


def behavior_predict(code: str, source: str | Path, model_dir: str | Path, *, threshold: float | None = None) -> dict[str, Any]:
    """Run a clean behavior model and return review candidates in memory."""
    return predict_behavior(code, source, model_dir, threshold=threshold)


def candidate_queue(pack: dict[str, Any], records: list[dict[str, Any]], output: Path, cache_dir: Path, *, cancelled=lambda: False, progress=lambda *_: None) -> dict[str, Any]:
    """Generate review-only candidates; never writes human labels."""
    return run_candidate_scan(pack, records, output, cache_dir, cancelled=cancelled, progress=progress)


def available_model_packs() -> list[dict[str, Any]]:
    """Return model packs visible to the application UI."""
    ensure_model_layout()
    return available_packs()


def inspect_model(*args, **kwargs):
    """Route single-record inspection through the application layer."""
    from cowmata_tailring.workspace.event_models import predict_one
    return predict_one(*args, **kwargs)


def installed_suites() -> list[dict[str, Any]]:
    ensure_model_layout()
    return list_suites(model_home())


def selected_suite() -> dict[str, Any] | None:
    ensure_model_layout()
    return active_suite(model_home())


__all__ = [
    "activate", "active_suite", "algorithm_output_root",
    "behavior_predict",
    "config_path", "available_model_packs", "candidate_queue",
    "default_home", "dataset_root", "ensure_model_layout", "install_suite",
    "inspect_model", "installed_suites", "job", "list_suites", "model_home",
    "read_settings", "runtime_roots", "save_settings", "selected_suite",
]
