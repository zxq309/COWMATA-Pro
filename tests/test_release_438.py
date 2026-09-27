"""4.3.8 COWMATA Annotator regression tests."""
import json

import numpy as np


def _suite(home, version):
    folder = home / "versions" / version
    folder.mkdir(parents=True)
    (folder / "suite.json").write_text("{}", encoding="utf-8")
    return folder


def test_model_library_discovery_picks_newest_version(monkeypatch, tmp_path):
    from cowmata_tailring.algorithms import paths

    lib = tmp_path / "科牧特_模型"
    _suite(lib / "4.3.7" / "行为识别", "a")
    _suite(lib / "4.3.10" / "行为识别", "b")
    (lib / "4.3.9").mkdir(parents=True)  # a version without behaviour models is skipped
    monkeypatch.delenv("COWMATA_ALGORITHM_HOME", raising=False)
    monkeypatch.setenv("COWMATA_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("COWMATA_MODEL_LIBRARY", str(lib))
    paths._DISCOVERY.clear()
    assert paths.model_home() == (lib / "4.3.10" / "行为识别").resolve()
    monkeypatch.delenv("COWMATA_MODEL_LIBRARY")
    monkeypatch.setenv("COWMATA_MODEL_DISCOVERY", "0")
    paths._DISCOVERY.clear()
    assert paths.model_home() == (tmp_path / "data" / "models" / "行为识别").resolve()


def test_reading_a_model_library_creates_no_folders(monkeypatch, tmp_path):
    from cowmata_tailring.algorithms.registry import active_suite, list_suites

    home = tmp_path / "lib"
    home.mkdir()
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(home))
    assert list_suites() == [] and active_suite() is None
    assert list(home.iterdir()) == []


def test_sample_weight_changes_the_fit_and_cpu_scoring_of_gpu_models():
    from cowmata_engine.decision.models import fit_model, predict_model

    rng = np.random.default_rng(1)
    x = rng.normal(size=(400, 3))
    y = (x[:, 0] + rng.normal(scale=.5, size=400) > 0).astype(int)
    a = fit_model("logistic", x, y, ["a", "b", "c"])
    w = np.where(x[:, 1] > 0, 0.05, 1.0)
    b = fit_model("logistic", x, y, ["a", "b", "c"], sample_weight=w)
    assert a["coef"] != b["coef"]
    xg = fit_model("xgboost", x, y, ["a", "b", "c"])
    p = predict_model(xg, x)
    assert p.shape == (400,) and np.all((p >= 0) & (p <= 1))


def test_strict_and_active_alert_rules():
    from cowmata_engine.decision.train438 import event_eval, smooth_by_cow

    h = 3_600_000
    onset = 100 * h
    rows = [dict(cow_id="1", calving_epoch_ms=onset, decision_epoch_ms=t * h, hours_to_calving=(onset - t * h) / h,
                 **{"coverage.lying_ratio@6h": 1.0}) for t in range(40, 100)]
    prob = np.zeros(len(rows))
    prob[[i for i, r in enumerate(rows) if 80 <= r["decision_epoch_ms"] // h <= 95]] = 0.9  # starts 20 h before onset
    active = event_eval(rows, prob, 0.5, persistence=2, window_h=12, rule="active")
    strict = event_eval(rows, prob, 0.5, persistence=2, window_h=12, rule="start")
    assert active["detected"] == 1 and strict["detected"] == 0 and strict["false_alerts"] == 1
    sm = smooth_by_cow(rows, prob, 3)
    assert sm[0] == 0 and 0 < sm[40] < 0.9


def test_behavior_events_is_the_ninth_causal_feature():
    from cowmata_engine.decision.models import PRIORS
    from cowmata_engine.features import FEATURE_MODULES, load_feature

    spec = load_feature("behavior_events").SPEC
    assert len(FEATURE_MODULES) == 9 and spec.modality == "motion" and spec.lookahead_ms > 0
    assert "behavior_events" in PRIORS and spec.primary in spec.columns


def test_annotator_branding_and_ledger_onset_shift():
    from cowmata_engine.decision.labels import LEDGER_ONSET_OFFSET_MS, merge_calvings
    from cowmata_tailring import __version__

    assert __version__ == "4.3.8"
    item = merge_calvings({"7": [10_000_000]}, {})["7"][0]
    assert item["start_epoch_ms"] == 10_000_000 - LEDGER_ONSET_OFFSET_MS and item["training_eligible"] is False
