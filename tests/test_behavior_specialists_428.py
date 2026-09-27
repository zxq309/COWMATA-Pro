import hashlib
import json

import numpy as np


def test_standup_linear_training_exports_detector_manifest(tmp_path):
    from cowmata_tailring.algorithms.behavior.standup.features import FEATURE_NAMES
    from cowmata_tailring.algorithms.behavior.standup.train import train_arrays
    from cowmata_tailring.algorithms.behavior.standup.detector import MODEL_SCHEMA

    rng = np.random.default_rng(428)
    X = rng.normal(size=(48, len(FEATURE_NAMES)))
    y = np.repeat([0, 1, 2], 16)
    manifest = train_arrays(X, y, output_dir=tmp_path, version="test-standup")
    assert manifest["schema"] == MODEL_SCHEMA
    assert manifest["lr"]["features"]
    saved = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert saved["version"] == "test-standup"


def test_urination_forest_json_roundtrip():
    from cowmata_tailring.algorithms.behavior.urination.forest import fit_forest, export_forest, predict_forest
    from cowmata_tailring.algorithms.behavior.urination.features import feature_names

    rng = np.random.default_rng(428)
    names = feature_names()
    X = rng.normal(size=(32, len(names))).astype(np.float32)
    y = np.array([0, 1] * 16)
    model, med = fit_forest(X, y, np.ones(len(y)), n_estimators=8, min_samples_leaf=1)
    payload = export_forest(model, names, med)
    score = predict_forest(payload, X[:5])
    assert payload["schema"] == "numeric-forest-1"
    assert score.shape == (5,)
    assert np.all(np.isfinite(score))


def test_urination_candidate_api_is_pure():
    from cowmata_tailring.algorithms.behavior.urination.candidates import generate

    n = 400
    acc = np.zeros((n, 3), dtype=np.float32)
    acc[:, 1] = -1
    valid = np.ones(n, dtype=bool)
    result = generate(acc, valid)
    assert isinstance(result, list)
