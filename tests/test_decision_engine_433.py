"""4.3.3 headless decision engine: contract, causality, training, JSON models and API."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from cowmata_engine.decision import dataset as ds
from cowmata_engine.decision.models import fit_model, predict_model
from cowmata_engine.features.base import FeatureSpec, empty_row, validate_rows, window_grid

CHINA = timezone(timedelta(hours=8))
HOUR = 3_600_000
SPECS = {
    "temperature": ["temperature_c"],
    "activity": ["activity_index"],
    "straining_ratio": ["straining_ratio"],
    "lying_ratio": ["lying_ratio"],
}


def synthetic(root: Path, cows=8, days=8, seed=4):
    rng = np.random.default_rng(seed)
    features = root / "Features"
    ledger = root / "ledger.csv"
    births = {}
    tables = {k: [] for k in SPECS}
    base = datetime(2026, 8, 20, tzinfo=CHINA)
    for n in range(cows):
        cow = str(21000 + n)
        calving = base + timedelta(days=days, hours=int(rng.integers(0, 24)))
        births[cow] = calving
        start = int((calving - timedelta(days=days)).timestamp() * 1000)
        end = int(calving.timestamp() * 1000)
        for s, e in window_grid(start, end - 1):
            h = (end - e) / HOUR
            local = ((e / HOUR) + 8) % 24
            temp = 38.6 + 0.2 * math.sin(2 * math.pi * local / 24) - (0.45 if h < 20 else 0) + rng.normal(0, 0.05)
            act = 1.0 + 0.3 * math.sin(2 * math.pi * local / 24) + (0.8 if h < 8 else 0) + rng.normal(0, 0.15)
            strain = (0.3 if h < 3 else 0.0) + abs(rng.normal(0, 0.01))
            lying = 0.5 + 0.1 * math.cos(2 * math.pi * local / 24) + rng.normal(0, 0.05)
            values = dict(temperature=temp, activity=act, straining_ratio=strain, lying_ratio=lying)
            for key, cols in SPECS.items():
                if key == "temperature" and rng.random() < 0.05:
                    continue  # realistic gaps
                tables[key].append(dict(cow_id=cow, device_id="0C3D5EA22DD" + str(n % 10), field_mark="A" + str(n),
                                        start_epoch_ms=s, end_epoch_ms=e, available_epoch_ms=e, coverage=1.0,
                                        **{cols[0]: values[key]}, source=f"{cow}.json"))
    for key, rows in tables.items():
        folder = features / key
        folder.mkdir(parents=True)
        cols = ["cow_id", "device_id", "field_mark", "start_epoch_ms", "end_epoch_ms", "available_epoch_ms",
                "coverage", *SPECS[key], "source"]
        with (folder / "windows.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, cols)
            writer.writeheader()
            writer.writerows(rows)
        (folder / "feature-manifest.json").write_text(json.dumps(dict(key=key, version=key + "-test", columns=SPECS[key])),
                                                      encoding="utf-8")
    with ledger.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ["生产日期", "牛场登记生产时间", "牛号", "已删除"])
        writer.writeheader()
        for cow, when in births.items():
            writer.writerow({"生产日期": when.strftime("%Y-%m-%d"), "牛场登记生产时间": when.strftime("%H:%M:%S"),
                             "牛号": cow, "已删除": "0"})
    return features, ledger, births


def test_contract_validation_rejects_acausal_rows():
    spec = FeatureSpec(key="demo", title="演示", modality="motion", version="1", columns=("x",), primary="x")
    row = empty_row(spec, 0, 600000)
    assert validate_rows(spec, [row])
    bad = dict(row, available_epoch_ms=100)
    with pytest.raises(ValueError):
        validate_rows(spec, [bad])
    with pytest.raises(ValueError):
        validate_rows(spec, [dict(row, x=float("nan"))])
    assert window_grid(1_200_001, 1_800_000) == [(1_200_000, 1_800_000), (1_800_000, 2_400_000)]


def test_derivations_are_causal():
    values = np.r_[np.zeros(600), np.ones(200)]
    parts, _ = ds.derive_series(values, np.ones_like(values))
    # Changing the future must not change any derivation at slot 599.
    future = np.r_[np.zeros(600), np.full(200, 50.0)]
    later, _ = ds.derive_series(future, np.ones_like(future))
    for key in parts:
        a, b = parts[key][:600], later[key][:600]
        assert np.allclose(np.nan_to_num(a), np.nan_to_num(b)), key
    assert parts["d24"][640] > 0.5 and parts["z72"][640] > 3


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    from cowmata_engine.decision.train import train_decision

    root = tmp_path_factory.mktemp("decision433")
    features, ledger, births = synthetic(root)
    summary = ds.build_dataset(output=root / "set", features_root=features, ledger=ledger)
    report = train_decision(root / "set", root / "model",
                            algorithms=("expert_rules", "logistic", "random_forest", "xgboost"), folds=4)
    return root, summary, report


def test_build_train_predict_end_to_end(trained):
    from cowmata_engine.decision.predict import predict_rows

    root, summary, report = trained
    assert summary["calving_cows"] == 8 and summary["positives"]["24h"] > 0
    assert any(p for p in summary["profile"]["temperature_c"])
    best = report["leaderboard"][0]
    assert best["metrics"]["roc_auc"] > 0.8
    assert report["events"]["event_sensitivity"] >= 0.5
    assert report["group_importance"] and report["learning_curve"]
    assert any(item["fold_curves"] for item in report["leaderboard"] if item["key"] == "xgboost")
    rows = ds.read_table(root / "set")
    result = predict_rows(rows[:300], root / "model")
    first = result["rows"][0]
    assert set(first["risk"]) == {"6h", "12h", "24h", "48h"}
    assert first["risk"]["6h"] <= first["risk"]["12h"] <= first["risk"]["24h"] <= first["risk"]["48h"]
    assert first["warning_level"] in {"正常", "关注", "高度关注", "临产", "数据不足"}
    assert "hours_to_calving_p50" in first
    assert "heart_rate" not in result["used_features"]


def test_predict_windows_and_http_service(trained):
    import threading
    import urllib.request

    from cowmata_engine import api
    from cowmata_engine.server import make_server

    root, _, _ = trained
    windows = {}
    for key in SPECS:
        with (root / "Features" / key / "windows.csv").open(encoding="utf-8-sig") as stream:
            rows = [r for r in csv.DictReader(stream) if r["cow_id"] == "21000"]
        windows[key] = [dict(cow_id=r["cow_id"], device_id=r["device_id"], start_epoch_ms=int(r["start_epoch_ms"]),
                             end_epoch_ms=int(r["end_epoch_ms"]), **{c: float(r[c]) for c in SPECS[key]}) for r in rows]
    response = api.handle(dict(action="decision.predict_windows", windows=windows, model=str(root / "model")))
    assert response["ok"], response.get("error")
    assert response["result"]["rows"] and response["result"]["alert_episodes"]
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=10) as reply:
            assert json.loads(reply.read())["ok"]
        body = json.dumps(dict(windows=windows, model=str(root / "model"))).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/decision.predict_windows", data=body, method="POST")
        with urllib.request.urlopen(request, timeout=60) as reply:
            payload = json.loads(reply.read())
        assert payload["ok"] and payload["result"]["rows"][0]["model_version"]
    finally:
        server.shutdown()
        server.server_close()


def test_decision_window_renders_training_and_prediction(qt_application, trained, tmp_path, monkeypatch):
    from cowmata_engine.decision.predict import predict_rows
    from cowmata_tailring.ui.algorithms.decision_ui import DecisionWindow

    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path / "models" / "行为识别"))
    root, summary, report = trained
    window = DecisionWindow()
    window.show_dataset(summary, root / "set")
    window.show_report(report)
    window._set_model(root / "model", quiet=True)
    window.show_prediction(predict_rows(ds.read_table(root / "set")[:200], root / "model"))
    assert window.leaderboard.rowCount() == 4 and window.folder_table.rowCount() == 200
    assert window.roc_chart.series and window.loss_chart is not None
    assert window.feature_table.rowCount() == 7
    calls = []
    monkeypatch.setattr(window, "launch", lambda request: calls.append(request))
    window.dataset_file.setText(str(root / "set"))
    window.train()
    assert calls and calls[-1]["engine"]["action"] == "decision.train"
    assert set(calls[-1]["engine"]["algorithms"]) >= {"xgboost", "stacking"}
    window.close()
    window.deleteLater()


@pytest.mark.parametrize("algorithm", ["decision_tree", "extra_trees", "adaboost", "baseline_deviation", "stacking"])
def test_json_models_round_trip(algorithm):
    rng = np.random.default_rng(1)
    x = rng.normal(size=(400, 4))
    x[rng.random(x.shape) < 0.05] = np.nan
    y = (np.nan_to_num(x[:, 0]) + 0.5 * np.nan_to_num(x[:, 1]) > 0.8).astype(int)
    columns = ["temperature_c@z72", "activity_index@z72", "b@1h", "c@6h"]
    doc = fit_model(algorithm, x, y, columns, groups=np.arange(400) % 8,
                    primaries={"temperature": "temperature_c", "activity": "activity_index"})
    again = json.loads(json.dumps(doc))
    p = predict_model(again, x)
    assert p.shape == (400,) and np.all((p >= 0) & (p <= 1))
    from sklearn.metrics import roc_auc_score

    assert roc_auc_score(y, p) > 0.7


def test_columns_present_even_when_first_cow_lacks_a_feature():
    base = 1_787_000_000_000 // 600_000 * 600_000
    temp = [dict(cow_id=c, device_id="D", field_mark="", start_epoch_ms=base + i * 600_000,
                 end_epoch_ms=base + (i + 1) * 600_000, available_epoch_ms=base + (i + 1) * 600_000, coverage=1.0,
                 temperature_c=38.5) for c in ("1", "2") for i in range(60)]
    act = [dict(r, activity_index=1.0) for r in temp if r["cow_id"] == "2"]
    rows = ds.build_decision_rows({"temperature": temp, "activity": act},
                                  {"temperature": dict(columns=["temperature_c"]), "activity": dict(columns=["activity_index"])})
    first = [r for r in rows if r["cow_id"] == "1"][0]
    assert "activity_index@1h" in first and first["activity_index@1h"] is None
    assert "activity_index@1h" in ds.model_columns(rows)


def test_decision_table_keeps_coverage_columns(trained):
    root, _, _ = trained
    rows = ds.read_table(root / "set")
    assert "coverage.temperature@6h" in rows[0] and rows[0]["coverage.temperature@6h"] is not None
    assert not any(c.startswith("coverage.") for c in ds.model_columns(rows))


def test_api_dispatch_and_catalog(tmp_path):
    from cowmata_engine import api

    info = api.handle(dict(action="engine.info"))
    assert info["ok"] and "decision.train" in info["result"]["actions"]
    catalog = api.handle(dict(action="features.catalog"))["result"]
    assert {r["key"] for r in catalog} >= {"heart_rate", "spo2", "activity", "temperature", "lying_ratio",
                                          "straining_ratio", "gyro_spectral_entropy"}
    bad = api.handle(dict(action="nope"))
    assert not bad["ok"] and bad["error"]
