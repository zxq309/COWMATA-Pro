"""Runtime paths shared by algorithm, service, and UI layers.

The application never assumes a developer drive. Models, caches, training
runs, and review queues are external state selected by environment variables
or by the portable data directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]


def _configured(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def data_root() -> Path:
    configured = _configured("COWMATA_DATA_HOME")
    if configured is not None:
        return configured
    if os.environ.get("COWMATA_PORTABLE_DATA", "").strip().lower() in {"1", "true", "yes"}:
        return (APP_ROOT / "data").resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data).expanduser() if local_app_data else Path.home()
    return (base / "COWMATA Pro" / "data").resolve()


def model_home() -> Path:
    configured = _configured("COWMATA_ALGORITHM_HOME")
    if configured is not None:
        return configured
    saved = read_settings().get("model_home")
    if isinstance(saved, str) and saved.strip():
        return Path(saved).expanduser().resolve()
    return data_root() / "models" / "行为识别"


def algorithm_output_root() -> Path:
    configured = _configured("COWMATA_ALGORITHM_OUTPUT")
    if configured is not None:
        return configured
    return model_home() / "runs"


def dataset_root() -> Path | None:
    configured = _configured("COWMATA_DATASET_HOME")
    if configured is not None:
        return configured
    return None


def ensure_runtime_layout(root: Path | None = None) -> Path:
    home = (root or model_home()).resolve()
    for name in ("versions", "cache", "jobs", "runs", "review_queue", "experiments"):
        (home / name).mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    """Persistent runtime settings file outside the shipped source tree."""
    return data_root() / "settings.json"


def read_settings() -> dict[str, object]:
    target = config_path()
    if not target.is_file():
        return {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_settings(settings: dict[str, object]) -> Path:
    if not isinstance(settings, dict):
        raise TypeError("settings must be a mapping")
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return target
