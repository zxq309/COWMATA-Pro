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


# 4.4.1 layout: <drive>\3_训练器\产犊\模型\<version>\行为识别 (训练器 owns datasets, models and results).
MODEL_LIBRARY = ("3_训练器", "产犊", "模型")
BEHAVIOR_FOLDER = "行为识别"


def library_under(base: Path) -> Path:
    return Path(base).joinpath(*MODEL_LIBRARY)


def _version_key(name: str):
    parts = []
    for piece in str(name).replace("-", ".").split("."):
        parts.append((0, int(piece), "") if piece.isdigit() else (1, 0, piece))
    return parts


def has_suites(home: Path) -> bool:
    try:
        return any((Path(home) / "versions").glob("*/suite.json"))
    except OSError:
        return False


def discover_model_homes() -> list[Path]:
    """Behaviour-model folders of the versioned model library, newest version first.

    4.4.1 layout: ``<drive>\\3_训练器\\产犊\\模型\\<version>\\行为识别\\versions\\<suite>\\suite.json``.
    Roots searched: the application's parent folders (e.g. …\\2_标注器\\…) and every local drive root.
    """
    import time

    explicit = os.environ.get("COWMATA_MODEL_LIBRARY", "").strip()
    if os.environ.get("COWMATA_MODEL_DISCOVERY", "1").strip().lower() in {"0", "false", "no"} and not explicit:
        return []
    key = explicit or "auto"
    cached = _DISCOVERY.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return list(cached[1])
    if explicit:
        roots = [Path(explicit).expanduser()]
    else:
        roots = [library_under(base) for base in APP_ROOT.parents]
        if os.name == "nt":
            import ctypes
            import string

            mask = ctypes.windll.kernel32.GetLogicalDrives()
            # Local fixed / removable drives only (DRIVE_REMOVABLE=2, DRIVE_FIXED=3): network drives may hang.
            for i, letter in enumerate(string.ascii_uppercase):
                if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") in (2, 3):
                    roots.append(library_under(Path(f"{letter}:\\")))
    found, seen = [], set()
    for library in roots:
        try:
            if not library.is_dir():
                continue
            versions = sorted((p for p in library.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name), reverse=True)
        except OSError:
            continue
        for folder in versions:
            home = (folder / BEHAVIOR_FOLDER).resolve()
            if home not in seen and has_suites(home):
                seen.add(home)
                found.append(home)
    _DISCOVERY[key] = (time.monotonic(), list(found))
    return found


_DISCOVERY: dict[str, tuple[float, list[Path]]] = {}


def model_home() -> Path:
    configured = _configured("COWMATA_ALGORITHM_HOME")
    if configured is not None:
        return configured
    saved = read_settings().get("model_home")
    if isinstance(saved, str) and saved.strip():
        return Path(saved).expanduser().resolve()
    local = data_root() / "models" / BEHAVIOR_FOLDER
    if has_suites(local):
        return local
    discovered = discover_model_homes()
    return discovered[0] if discovered else local


def algorithm_output_root() -> Path:
    configured = _configured("COWMATA_ALGORITHM_OUTPUT")
    if configured is not None:
        return configured
    # 4.3.8: caches never go into the (possibly shared, read-only) model library.
    return data_root() / "algorithm_runs"


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
