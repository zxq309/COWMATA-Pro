"""Heart-rate (心率) decision feature: PPG pulse-rate estimate, causal engine, plug-ins."""
import base64
import json

import numpy as np
import pytest

from cowmata_tailring.algorithms.heart_rate import (
    FEATURE_COLUMNS,
    HeartRateModule,
    as_fusion_features,
    attach_heart_rate,
    measure_document,
    measurement_timing,
)
from cowmata_tailring.algorithms.heart_rate_signal import estimate_document

HOUR = 3_600_000
T0 = 1_788_000_000_000


def ppg_document(bpm=72.0, *, seconds=90, noise=0.05, seed=0, create=T0, lag_ms=3_711_000, pulse=True):
    rng = np.random.default_rng(seed)
    fs = 100
    t = np.arange(seconds * fs) / fs
    phase = 2 * np.pi * bpm / 60 * t
    wave = (np.sin(phase) + 0.45 * np.sin(2 * phase - 0.8) + 0.15 * np.sin(3 * phase - 1.4)) if pulse else 0 * t
    ir = 60000 + 400 * wave + 400 * noise * rng.standard_normal(len(t)) + 300 * np.sin(2 * np.pi * 0.05 * t)
    red = 25000 + 150 * wave + 150 * noise * rng.standard_normal(len(t))
    config = dict(pulse_led_mode=2, pulse_led_sr=3, pulse_led_avr=2, pulse_sample_time=seconds, work_mode=6)
    return dict(configs=json.dumps(config), create_time=create, update_time=create, time=create - lag_ms,
                device="0C3D5EA22E3B", uid=str(seed), led=-50,
                ir_data=base64.b64encode(ir.astype("<u4").tobytes()).decode(),
                data=base64.b64encode(red.astype("<u4").tobytes()).decode())


@pytest.mark.parametrize("bpm", [55.0, 72.0, 96.0, 124.0])
def test_clean_pulse_rate_is_recovered_with_high_grade(bpm):
    result = estimate_document(ppg_document(bpm))
    assert result["grade"] == "HIGH"
    assert abs(result["heart_rate_bpm"] - bpm) < 1.5
    assert abs(result["beat_bpm"] - bpm) < 2.0


def test_noise_without_pulse_is_never_reported_as_heart_rate():
    for seed in range(5):
        result = estimate_document(ppg_document(pulse=False, noise=1.0, seed=seed))
        assert result["grade"] in ("LOW", "REJECT")


def test_off_skin_signal_is_rejected():
    doc = ppg_document()
    doc["ir_data"] = base64.b64encode(np.full(9000, 500, dtype="<u4").tobytes()).decode()
    assert estimate_document(doc)["reason"] == "NO_SKIN_CONTACT"


def test_capture_time_uses_device_time_and_availability_uses_upload():
    sample, available, basis = measurement_timing(ppg_document())
    assert (sample, available, basis) == (T0 - 3_711_000, T0, "device_time")
    doc = ppg_document(lag_ms=0)
    assert measurement_timing(doc)[2] == "create_time_upper_bound"


def series(values, start=T0, step=int(66.6 * 60000)):
    out = []
    for i, bpm in enumerate(values):
        create = start + i * step
        m = measure_document(ppg_document(bpm, seed=i, create=create), source=f"f{i}", source_sha256=f"s{i}")
        out.append(m)
    return out


def test_engine_is_causal_and_detects_rise_against_own_baseline():
    base = [75.0 + (i % 3) for i in range(90)]  # ~100 h at the 66.6 min cadence
    late = [92.0] * 8
    ms = series(base + late)
    module = HeartRateModule("23158", "0C3D5EA22E3B")
    for m in ms[:len(base)]:
        module.add_measurement(m)
    before = module.evaluate(ms[len(base) - 1]["available_epoch_ms"])
    assert before["quality_status"] == "VALID"
    assert abs(before["features"]["hr_rise_bpm"]) < 6
    for m in ms[len(base):]:
        module.add_measurement(m)
    # A capture uploaded after the evaluation time must not be visible.
    cut = ms[len(base) + 3]["available_epoch_ms"]
    mid = module.evaluate(cut)
    after = module.evaluate(ms[-1]["available_epoch_ms"])
    assert mid["features"]["hr_rise_bpm"] < after["features"]["hr_rise_bpm"]
    assert after["features"]["hr_rise_bpm"] > 12
    assert after["heart_rate_evidence_level"] in ("ELEVATED", "STRONG")
    assert after["calving_probability"] is None
    with pytest.raises(ValueError):
        module.evaluate(cut)
    fused = as_fusion_features(after)
    assert fused["heart_rate_available"] and set(FEATURE_COLUMNS) <= set(fused)


def test_short_history_is_not_usable_for_fusion():
    ms = series([80.0] * 5)
    module = HeartRateModule("1", "0C3D5EA22E3B")
    for m in ms:
        module.add_measurement(m)
    result = module.evaluate(ms[-1]["available_epoch_ms"])
    assert result["quality_status"] == "INSUFFICIENT_HISTORY"
    assert result["features"]["hr_rise_bpm"] is None and result["heart_rate_evidence_level"] == "UNAVAILABLE"
    assert result["features"]["heart_rate_bpm"] is not None


def test_state_round_trip_and_duplicate_capture():
    ms = series([78.0] * 12)
    module = HeartRateModule("1", "0C3D5EA22E3B")
    for m in ms:
        module.add_measurement(m)
    assert module.add_measurement(ms[0]) == "DUPLICATE_IGNORED"
    clone = HeartRateModule.from_state_json(module.to_state_json())
    now = ms[-1]["available_epoch_ms"] + 1
    assert clone.evaluate(now)["features"] == module.evaluate(now)["features"]


def test_decision_feature_plugin_contract(tmp_path):
    from cowmata_engine.features import load_feature, validate_rows

    plugin = load_feature("heart_rate")
    files = []
    for i, doc in enumerate(ppg_document(76.0 + (i % 2), seed=i, create=T0 + i * 3_996_000) for i in range(40)):
        file = tmp_path / f"{i:03d}.json"
        file.write_text(json.dumps(doc))
        files.append(str(file))
    rows = plugin.extract_series(files)
    validate_rows(plugin.SPEC, rows)
    assert rows and plugin.SPEC.primary in plugin.SPEC.columns
    first_upload = T0
    # Every window from the first capture's sampling window onwards is present, gaps included.
    starts = [r["start_epoch_ms"] for r in rows]
    assert starts == list(range(starts[0], starts[-1] + 600_000, 600_000))
    assert starts[0] <= T0 - 3_711_000 < starts[0] + 600_000
    # Causal: nothing is known before the first upload reached the server.
    before = [r for r in rows if r["end_epoch_ms"] < first_upload]
    assert before and all(all(r[c] is None for c in plugin.SPEC.columns) and r["coverage"] == 0 for r in before)
    assert any(r["hr_rise_bpm"] is not None for r in rows)
    single = plugin.extract(files[0])
    validate_rows(plugin.SPEC, single)
    assert single[0]["available_epoch_ms"] >= single[0]["end_epoch_ms"]


def test_plugin_keeps_rows_across_gaps_and_uses_history_only_as_context():
    from cowmata_engine.features import heart_rate as plugin
    from cowmata_tailring.algorithms.heart_rate import Config

    ms = series([78.0 + (i % 3) for i in range(60)])
    history, target = ms[:40], ms[40:45] + ms[55:]  # ~11 h gap inside the target span
    rows = plugin.rows_from_measurements(target, history=history, cow_id="1", device_id="0C3D5EA22E3B")
    starts = [r["start_epoch_ms"] for r in rows]
    assert starts == list(range(starts[0], starts[-1] + 600_000, 600_000))
    assert starts[0] <= target[0]["sample_epoch_ms"] < starts[0] + 600_000
    assert rows[-1]["end_epoch_ms"] > target[-1]["available_epoch_ms"] >= rows[-1]["start_epoch_ms"]
    gap = [r for r in rows if ms[45]["available_epoch_ms"] + 4 * HOUR < r["end_epoch_ms"] < ms[55]["available_epoch_ms"]]
    assert gap and all(r["coverage"] == 0 and r["heart_rate_bpm"] is None for r in gap)
    assert rows[0]["hr_rise_bpm"] is not None  # baseline comes from the earlier history
    alone = plugin.rows_from_measurements(target, cow_id="1", device_id="0C3D5EA22E3B")
    assert [r["start_epoch_ms"] for r in alone] == starts and alone[0]["hr_rise_bpm"] is None
    flat = plugin.rows_from_measurements(target, history=history, cow_id="1", device_id="0C3D5EA22E3B",
                                         config=Config(circadian_bpm=(0.0,) * 24))
    assert flat[0]["hr_level_6h_bpm"] != rows[0]["hr_level_6h_bpm"]
    with pytest.raises(ValueError):
        Config(circadian_bpm=(0.0,) * 23)


def test_fusion_rows_receive_heart_rate_only_after_upload(tmp_path):
    file = tmp_path / "p.json"
    file.write_text(json.dumps(ppg_document(81.0)))
    record = dict(cow_id="23158", device_id="0C3D5EA22E3B", field_mark="H5", raw=str(file))
    rows = [dict(cow_id="23158", device_id="0C3D5EA22E3B", field_mark="H5", decision_epoch_ms=T0 - 60_000),
            dict(cow_id="23158", device_id="0C3D5EA22E3B", field_mark="H5", decision_epoch_ms=T0 + 60_000),
            dict(cow_id="9", device_id="X", field_mark="", decision_epoch_ms=T0)]
    assert attach_heart_rate(rows, [record]) == []
    assert rows[0].get("heart_rate_bpm") is None
    assert abs(rows[1]["heart_rate_bpm"] - 81.0) < 1.5
    assert rows[2]["heart_rate_bpm"] is None
