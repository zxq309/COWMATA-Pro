"""4.3.7 regression tests: PPG pulse feature, gold-only truth, fused decision, and bugs 4.3.4-1..6."""
from __future__ import annotations

import base64
import csv
import json
import math

import numpy as np
import pytest

from cowmata_engine.features import FEATURE_MODULES, load_feature, validate_rows
from cowmata_engine.features.base import WINDOW_MS

HOUR = 3_600_000


def ppg_document(t0_ms, *, hr=72.0, fs=100.0, seconds=90, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(fs * seconds)) / fs
    phase = 2 * np.pi * hr / 60.0 * t
    wave = np.sin(phase) + 0.35 * np.sin(2 * phase + 0.6) + 0.12 * np.sin(3 * phase + 1.1)
    wave = wave + noise * rng.standard_normal(len(t))
    red = (48000 - 300 * wave).astype("<u4")
    ir = (96000 - 600 * wave).astype("<u4")
    return {"uid": 1, "device": "546C50CA0001", "time": int(t0_ms), "create_time": int(t0_ms + 62 * 60000),
            "update_time": int(t0_ms + 62 * 60000),
            "configs": json.dumps({"pulse_sample_time": seconds}),
            "data": base64.b64encode(red.tobytes()).decode(), "ir_data": base64.b64encode(ir.tobytes()).decode()}


def test_ppg_pulse_is_registered_as_the_eighth_fused_feature():
    assert "ppg_pulse" in FEATURE_MODULES and list(FEATURE_MODULES)[-1] == "behavior_events" and len(FEATURE_MODULES) == 9  # 4.3.8
    spec = load_feature("ppg_pulse").SPEC
    assert spec.modality == "ppg" and spec.lookahead_ms == 0
    # No double counting with the heart-rate / SpO2 plug-ins.
    assert not {"heart_rate_bpm", "perfusion_index_percent"} & set(spec.columns)


def test_ppg_measurement_quality_gate_and_signal(tmp_path):
    from cowmata_tailring.algorithms.ppg_pulse import measure_file

    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps(ppg_document(1_787_000_000_000)))
    m = measure_file(clean)
    assert m["gate"] == "pass" and m["quality"]
    assert m["pulse_rate_bpm"] == pytest.approx(72.0, abs=2.0)
    assert m["available_epoch_ms"] > m["sample_epoch_ms"]  # known only after upload
    assert len(m["harmonic_ratio"]) == 11 and m["harmonic_c0"] > 0
    noisy = tmp_path / "noisy.json"
    noisy.write_text(json.dumps(ppg_document(1_787_000_000_000, noise=6.0, seed=3)))
    assert measure_file(noisy)["gate"] != "pass"


def test_ppg_windows_are_causal_and_follow_the_contract(tmp_path):
    module = load_feature("ppg_pulse")
    start = 1_787_000_000_000
    paths = []
    for k in range(6):
        p = tmp_path / f"c{k}.json"
        p.write_text(json.dumps(ppg_document(start + k * 66 * 60000, seed=k)))
        paths.append(str(p))
    rows = validate_rows(module.SPEC, module.extract_series(paths))
    assert rows and all(r["available_epoch_ms"] >= r["end_epoch_ms"] for r in rows)
    first_arrival = start + 62 * 60000 + 90_000
    for r in rows:
        if r["end_epoch_ms"] < first_arrival:
            assert r["ppg_rmssd_ms"] is None, "a window may not use a capture the server had not received"
    assert any(r["ppg_rmssd_ms"] is not None for r in rows)
    assert all(r["start_epoch_ms"] % WINDOW_MS == 0 for r in rows)


def test_read_table_parses_training_eligible_as_bool(tmp_path):
    """4.3.5 kept the string "False", which passed ``is not False`` and trained on ledger labels."""
    from cowmata_engine.decision.dataset import read_table

    path = tmp_path / "decision_table.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ["cow_id", "decision_epoch_ms", "training_eligible", "label_quality"])
        writer.writeheader()
        writer.writerow(dict(cow_id="1", decision_epoch_ms=0, training_eligible="False", label_quality="approximate"))
        writer.writerow(dict(cow_id="2", decision_epoch_ms=0, training_eligible="True", label_quality="gold"))
        writer.writerow(dict(cow_id="3", decision_epoch_ms=0, training_eligible="", label_quality=""))
    rows = read_table(path)
    assert [r["training_eligible"] for r in rows] == [False, True, None]


def test_alert_that_runs_into_calving_is_a_hit_not_a_false_alarm():
    from cowmata_engine.decision.train import event_metrics

    calving = 100 * HOUR
    rows, prob = [], []
    for h in range(60, 100):
        rows.append(dict(cow_id="a", calving_epoch_ms=calving, decision_epoch_ms=h * HOUR, hours_to_calving=(calving - h * HOUR) / HOUR))
        prob.append(0.9 if h >= 80 else 0.0)  # starts 20 h early and continues to T0
    result = event_metrics(rows, np.asarray(prob), 0.5, 12)
    assert result["detected"] == 1 and result["false_alerts"] == 0


def test_prediction_keeps_baseline_columns_and_monotone_horizons(tmp_path):
    from cowmata_engine.decision import predict

    source = open(predict.__file__, encoding="utf-8").read()
    assert 'endswith(("@d24", "@z72", "@circ"))' not in source, "train/serve skew must stay removed"
    assert "np.maximum.accumulate" in source


def test_rate_is_applied_even_without_compatibility_cache(qapp):
    from cowmata_tailring.workspace.playback import VideoBoard

    board = VideoBoard()
    board.timer.stop()
    notices = []
    board.notice.connect(notices.append)
    board.compatibility_failures.add("asset")

    class Tile:
        interval = object()
        asset_id = "asset"
        engine = None
    board.tiles = {"v": Tile()}
    board.set_rate(4)
    assert board.rate == 4 and notices
    with pytest.raises(ValueError):
        board.set_rate("fast")
    board.tiles = {}
    board.close()


def test_enlarged_view_gets_more_height(qapp):
    from PySide6.QtWidgets import QWidget

    from cowmata_tailring.workspace.presentation import PresentationVideoBoard, WorkspaceStage

    class Signals(QWidget):
        def __init__(self):
            super().__init__()
            self.toolbar = QWidget(self)
            self.scroll = QWidget(self)
            self.wave = QWidget(self)

    board = PresentationVideoBoard()
    board.timer.stop()
    stage = WorkspaceStage(board, Signals())
    stage.resize(1200, 900)
    stage.set_mode("A")
    board.select(["v1", "v2", "v3"])
    stage.arrange()
    before = stage.video.height()
    board.enlarge("v1")
    after = stage.video.height()
    assert after > before * 1.3, (before, after)
    board.enlarge(None)
    assert stage.video.height() == before
    board.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])