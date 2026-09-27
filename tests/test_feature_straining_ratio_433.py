"""努责占比 decision feature: contract, estimator arithmetic and failure modes (no real data needed)."""
import json

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_engine.features import straining_ratio as SR

WIN = 600_000
E0 = 1_787_900_400_000  # multiple of 10 min


def _params(**over):
    p = dict(schema=SR.PARAMS_SCHEMA, stage2_threshold=0.2, tpr=0.6, fpr=0.05,
             core=dict(gap_s=9.0, pad_before_s=0.5, pad_after_s=1.0), recurrence_window_ms=300_000,
             bout_calibration=dict(x=[0.0, 1.0], y=[0.0, 1.0]))
    p.update(over)
    return p


def _record(bouts, pulses, seconds=1200, valid=None, e0=E0):
    sec = np.arange(seconds) + 0.5
    return dict(epoch0_ms=float(e0), seconds=sec, valid=np.ones(seconds, bool) if valid is None else valid,
                bout_start_ms=np.array([b[0] for b in bouts], float), bout_end_ms=np.array([b[1] for b in bouts], float),
                bout_p2=np.array([b[2] for b in bouts], float), quiet_pulse_t=np.asarray(pulses, float), source="x.json")


def test_plugin_contract():
    module = load_feature("straining_ratio")
    assert module.SPEC.primary == "straining_ratio" and module.SPEC.modality == "motion"
    assert module.SPEC.lookahead_ms >= 900_000
    assert set(module.SPEC.column_titles) == set(module.SPEC.columns)
    assert module.SPEC.derivations == ("1h", "6h", "slope6h")


def test_core_mask_follows_pulse_chains_and_splits_on_gaps():
    sec = np.arange(300) + 0.5
    pulses = [100, 103, 106, 130, 133]  # gap 24 s > 9 s splits the chain
    m = SR.core_mask(sec, [(90_000, 150_000)], pulses, 9.0, 0.5, 1.0)
    covered = sec[m]
    assert covered.min() >= 99.5 and covered.max() <= 134.0
    assert not m[(sec > 108) & (sec < 129)].any()
    assert SR.core_mask(sec, [(0, 50_000)], pulses, 9.0, 0.5, 1.0).sum() == 0


def test_window_share_is_error_corrected_and_contract_valid():
    pulses = np.arange(100, 161, 3.0)
    rec = _record([(95_000, 170_000, 0.9)], pulses)
    rows = SR.window_rows([rec], _params())
    validate_rows(SR.SPEC, rows)
    assert [r["start_epoch_ms"] for r in rows] == [E0, E0 + WIN]
    first = rows[0]
    covered = SR.core_mask(rec["seconds"], [(95_000, 170_000)], pulses, 9.0, 0.5, 1.0)[:600].sum()
    raw = covered * 0.9 / 600
    assert first["straining_ratio_raw"] == pytest.approx(raw)
    assert first["straining_ratio"] == pytest.approx((raw - 0.05) / 0.55)
    assert first["straining_seconds"] == pytest.approx(first["straining_ratio"] * 600)
    assert first["straining_bouts"] == 1 and first["straining_recurrent_bouts"] == 0
    assert first["straining_pulses_per_min"] == pytest.approx(len(pulses) / 10)
    assert first["available_epoch_ms"] == first["end_epoch_ms"] + SR.LOOKAHEAD_MS
    assert rows[1]["straining_ratio"] == 0.0 and rows[1]["straining_bouts"] == 0


def test_rejected_bouts_and_background_noise_clip_to_zero():
    rows = SR.window_rows([_record([(95_000, 170_000, 0.1)], np.arange(100, 161, 3.0))], _params())
    assert rows[0]["straining_ratio"] == 0.0 and rows[0]["straining_bouts"] == 0
    tiny = SR.window_rows([_record([(95_000, 99_000, 0.9)], [96.0])], _params())
    assert tiny[0]["straining_ratio_raw"] > 0 and tiny[0]["straining_ratio"] == 0.0


def test_recurrent_bouts_across_records_and_missing_coverage():
    a = _record([(100_000, 130_000, 0.8)], np.arange(101, 130, 3.0), seconds=600)
    b = _record([(20_000, 50_000, 0.8)], np.arange(21, 50, 3.0), seconds=600, e0=E0 + WIN)
    rows = SR.window_rows([b, a], _params())
    assert [r["straining_recurrent_bouts"] for r in rows] == [0, 0]  # 520 s apart > 5 min
    c = _record([(300_000, 330_000, 0.8)], np.arange(301, 330, 3.0), seconds=600)
    rows = SR.window_rows([a | {"bout_start_ms": np.array([100_000.0, 300_000.0]), "bout_end_ms": np.array([130_000.0, 330_000.0]),
                                "bout_p2": np.array([0.8, 0.8]), "quiet_pulse_t": np.r_[a["quiet_pulse_t"], c["quiet_pulse_t"]]}], _params())
    assert rows[0]["straining_recurrent_bouts"] == 2
    gap = _record([], [], seconds=600, valid=np.r_[np.ones(30, bool), np.zeros(570, bool)])
    row = SR.window_rows([gap], _params())[0]
    assert row["coverage"] == pytest.approx(0.05) and row["straining_ratio"] is None


def test_missing_model_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_STRAINING_RATIO_MODEL", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="努责占比模型"):
        SR.extract("nothing.json")


def test_params_bundle_mismatch_is_rejected(tmp_path, monkeypatch):
    (tmp_path / SR.PARAMS_FILE).write_text(json.dumps(_params(bundle_sha256="0" * 64)), encoding="utf-8")
    (tmp_path / SR.BUNDLE_FILE).write_text("{}", encoding="utf-8")
    SR._load.cache_clear()
    with pytest.raises(ValueError):
        SR.load_model(tmp_path)