"""4.4.6: time-resolved salience and per-horizon adaptive weights (training and inference use the same composite)."""
from __future__ import annotations

import csv
import json

import numpy as np

from cowmata_engine.decision import dataset as ds
from cowmata_engine.decision import salience
from test_decision_engine_433 import synthetic


def _toy(rng, n=4000):
    hours = rng.uniform(0, 200, n)
    late = rng.normal(0, 1, n) + np.where(hours <= 2, 3.0, 0.0)              # only the last 2 h
    early = rng.normal(0, 1, n) - np.where((hours > 6) & (hours <= 24), 1.5, 0.0)  # 6–24 h, lower
    noise = rng.normal(0, 1, n)
    return np.column_stack([late, early, noise]), hours


def test_profile_finds_when_each_feature_matters_and_weights_follow_the_horizon():
    rng = np.random.default_rng(446)
    x, hours = _toy(rng)
    columns = ["late", "early", "noise"]
    stats = salience.profile(x, hours, columns)
    assert stats["late"]["auc"][0] > 0.9 and stats["late"]["auc"][4] < 0.6
    assert stats["early"]["auc"][4] < 0.25 and stats["early"]["effect"][4] < -1.0
    w = salience.weights(stats, [1, 2, 3, 6, 12])
    assert list(w["1"])[0] == "late" and w["1"]["late"][1] == 1.0
    assert w["12"]["early"][0] > w["12"].get("late", [0])[0]  # 6–12 h rows outweigh the last hours
    assert w["12"]["early"][1] == -1.0 and "noise" not in w["1"]
    comp = salience.composite(x, columns, stats, w["2"])
    assert np.nanmean(comp[hours <= 2]) > np.nanmean(comp[hours > 48]) + 2


def test_apply_reproduces_the_training_composite_from_the_manifest():
    rng = np.random.default_rng(7)
    x, hours = _toy(rng, 1500)
    columns = ["late", "early", "noise"]
    stats = salience.profile(x, hours, columns)
    w = salience.weights(stats, [1, 12])
    block = salience.manifest(stats, w, {c: c for c in columns}, {"late": "努责"})
    rows = [dict(zip(columns, map(float, r))) for r in x[:50]]
    salience.apply(rows, block)
    expected = salience.composite(x[:50], columns, stats, w["1"])
    got = np.asarray([r["salience@1h"] for r in rows], float)
    assert np.allclose(got, expected, atol=1e-5)
    assert block["features"][0]["key"] in columns and len(block["features"][0]["auc"]) == len(salience.BINS)
    assert abs(sum(block["horizon_weights"]["1"].values()) - 1.0) < 1e-3


def test_cross_fit_never_uses_a_folds_own_labels():
    rng = np.random.default_rng(3)
    x, hours = _toy(rng, 2000)
    split = [(np.arange(1000, 2000), np.arange(0, 1000)), (np.arange(0, 1000), np.arange(1000, 2000))]
    out, stats, w = salience.cross_fit(x, hours, ["late", "early", "noise"], split, [1, 12])
    s0 = salience.profile(x[1000:], hours[1000:], ["late", "early", "noise"])
    expected = salience.composite(x[:1000], ["late", "early", "noise"], s0, salience.weights(s0, [1, 12])["1"])
    assert np.allclose(out[:1000, 0], expected, equal_nan=True)


def test_train_calving_deploys_salience_and_predict_uses_it(tmp_path):
    from cowmata_engine.decision.predict import predict_rows
    from cowmata_engine.decision.train438 import train_calving

    features, ledger, _ = synthetic(tmp_path, cows=10, days=6, seed=11)
    ds.build_dataset(output=tmp_path / "set", features_root=features, ledger=ledger)
    table = tmp_path / "set" / "decision_table.csv"
    with table.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    gold = sorted({r["cow_id"] for r in rows})[:5]
    for r in rows:
        if r["hours_to_calving"]:
            r["label_quality"] = "gold" if r["cow_id"] in gold else "approximate"
    with table.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)
    train_calving(tmp_path / "set", tmp_path / "model", algorithms=("random_forest",), folds=4, progress=lambda *_: None)
    manifest = json.loads((tmp_path / "model" / "decision.json").read_text(encoding="utf-8"))
    block = manifest["salience"]
    used = [c for c in manifest["columns"] if c.startswith("salience@")]
    # adaptive: the composites are model inputs only when they win on the ledger-cow selection protocol
    assert used == ([f"salience@{h}h" for h in (1, 2, 3, 6, 12)] if block["used_as_input"] else [])
    assert block["ablation"]["with_salience"]["auc"] and block["ablation"]["without_salience"]["ledger"]
    assert set(block["horizon_choice"]) == {"1", "2", "3", "6", "12"} and block["weights"]["1"]
    straining = [k for k, v in block["horizon_weights"]["1"].items() if k == "straining_ratio"]
    assert straining, block["horizon_weights"]["1"]
    result = predict_rows(ds.read_table(tmp_path / "set")[:120], tmp_path / "model")
    assert result["rows"] and all(np.isfinite(r["risk"]["1h"]) for r in result["rows"])
