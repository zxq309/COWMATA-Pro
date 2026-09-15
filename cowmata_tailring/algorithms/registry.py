"""Validated, reversible model versions outside the application installation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from cowmata_tailring.workspace.storage import atomic_json

from . import EVENT_CODES
from .features import FEATURE_VERSION

APP_ROOT = Path(__file__).resolve().parents[2]


def default_home():
    override = os.environ.get("COWMATA_ALGORITHM_HOME")
    if override:
        return Path(override).resolve()
    return (Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) /
            "COWMATA" / "algorithms").resolve()


def child(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Invalid algorithm file name")
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError("Algorithm path escapes its directory")
    return target


def read_suite(folder, *, verify=True):
    folder = Path(folder).resolve()
    doc = json.loads((folder / "suite.json").read_text(encoding="utf-8"))
    if (doc.get("schema") != "cowmata-event-suite-1" or doc.get("complete") is not True
            or doc.get("feature_version") != FEATURE_VERSION
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", str(doc.get("version", "")))):
        raise ValueError("Incomplete or incompatible event suite")
    models = doc.get("models", [])
    if len(models) != len(EVENT_CODES) or {m["code"] for m in models} != set(EVENT_CODES):
        raise ValueError("All three event models are required")
    child(folder, doc["report"])
    for model in models:
        filename = child(folder, model["file"])
        if not 0 < float(model["threshold"]) <= 1:
            raise ValueError("Invalid model threshold")
        if verify:
            content = filename.read_bytes()
            if hashlib.sha256(content).hexdigest() != model["sha256"]:
                raise ValueError("Model checksum mismatch: " + model["file"])
            payload = json.loads(content)
            if (payload.get("code") != model["code"]
                    or payload.get("feature_version") != FEATURE_VERSION
                    or payload.get("threshold") != model["threshold"]):
                raise ValueError("Model metadata mismatch")
            import numpy as np

            from .models import predict_forest
            predict_forest(payload, np.zeros((1, len(payload["features"]))))
    return {**doc, "root": folder,
            "hash": hashlib.sha256((folder / "suite.json").read_bytes()).hexdigest()}


def list_suites(home=None):
    home = Path(home) if home is not None else default_home()
    candidates = list((home / "versions").glob("*/suite.json"))
    candidates += list((APP_ROOT / "assets/algorithms").glob("*/suite.json"))
    result = {}
    for manifest in sorted(candidates):
        try:
            doc = read_suite(manifest.parent, verify=False)
            result.setdefault(doc["version"], doc)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result.values(), key=lambda x: x["version"], reverse=True)


def active_suite(home=None):
    home = Path(home) if home is not None else default_home()
    selected = home / "active.json"
    if selected.is_file():
        version = json.loads(selected.read_text(encoding="utf-8"))["version"]
        suite = next((s for s in list_suites(home) if s["version"] == version), None)
        if suite is None:
            raise ValueError("Selected model version is missing; choose a version in algorithm management")
        return suite
    # Installing a version for comparison must not activate it implicitly.
    for manifest in sorted((APP_ROOT / "assets/algorithms").glob("*/suite.json"), reverse=True):
        try:
            return read_suite(manifest.parent, verify=False)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def activate(home, version):
    home = Path(home)
    suite = next((s for s in list_suites(home) if s["version"] == version), None)
    if suite is None:
        raise ValueError("Unknown algorithm version")
    read_suite(suite["root"])
    atomic_json(home / "active.json", {"version": version})
    return suite


def install_suite(source, home=None, *, make_active=True):
    home = Path(home) if home is not None else default_home()
    suite = read_suite(source)
    versions = home / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    target = child(versions, suite["version"])
    if target.exists():
        if read_suite(target)["hash"] != suite["hash"]:
            raise ValueError("This version already exists with different contents; use a new version name")
    else:
        with tempfile.TemporaryDirectory(prefix=".install-", dir=home) as tmp:
            stage = Path(tmp) / suite["version"]
            stage.mkdir()
            for model in suite["models"]:
                shutil.copy2(child(source, model["file"]), stage / model["file"])
            shutil.copy2(Path(source) / "suite.json", stage / "suite.json")
            report = json.loads(child(source, suite["report"]).read_text(encoding="utf-8"))
            atomic_json(stage / suite["report"], {k: report[k] for k in
                        ("models", "elapsed_seconds") if k in report})
            read_suite(stage)
            stage.rename(target)
    if make_active:
        activate(home, suite["version"])
    return target
