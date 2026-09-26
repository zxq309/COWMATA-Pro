"""4.3.3 血氧 (SpO2): red/IR oximetry, cowmata-spo2-1 records and the engine plug-in."""
import base64
import json
from pathlib import Path

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_tailring.algorithms.spo2_features import SpO2Module, attach_spo2, measure_file
from cowmata_tailring.algorithms.spo2_signal import R_SATURATED, analyse_signal, spo2_from_ratio
from cowmata_tailring.spo2 import (
    CONTRACT,
    export_spo2_sources,
    ppg_spo2_record,
    ppg_timing,
    read_spo2_record,
    validate_contract,
)

FS = 100.0
DEVICE = "0C3D5EA22E0D"


def pulse_wave(n, bpm, seed=0):
    t = np.arange(n) / FS
    f = bpm / 60.0
    rng = np.random.default_rng(seed)
    wave = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t + 0.8)
    return wave / np.std(wave) + 0.02 * rng.standard_normal(n)


def channels(ratio, bpm=78, seconds=90, pi_ir=0.004, seed=0):
    p = pulse_wave(int(seconds * FS), bpm, seed)
    ir = 90000 * (1 - pi_ir * p)
    red = 50000 * (1 - ratio * pi_ir * p)
    return red, ir


def document(red, ir, *, create=1_788_000_000_000, lag_ms=3_700_000, device=DEVICE):
    cfg = dict(work_mode=6, pulse_led_mode=2, pulse_led_sr=3, pulse_led_avr=2, pulse_sample_time=len(ir) / FS)
    doc = dict(uid=1, device=device, configs=json.dumps(cfg), create_time=create, update_time=create,
               time=create - lag_ms, ir_data=base64.b64encode(np.asarray(ir, "<u4").tobytes()).decode())
    if red is not None:
        doc["data"] = base64.b64encode(np.asarray(red, "<u4").tobytes()).decode()
    return doc


def test_ratio_of_ratios_recovers_reference_curve():
    for ratio in (0.45, 0.60, 0.80):
        red, ir = channels(ratio)
        result = analyse_signal(red, ir, FS)
        assert result["quality"] == "good"
        assert result["ratio_r"] == pytest.approx(ratio, rel=0.03)
        assert result["spo2_percent"] == pytest.approx(float(spo2_from_ratio(ratio)), abs=0.6)
        assert result["heart_rate_bpm"] == pytest.approx(78, abs=2)


def test_curve_is_monotonic_and_saturates():
    r = np.linspace(0.1, 1.2, 200)
    s = spo2_from_ratio(r)
    assert np.all(np.diff(s) <= 1e-12)
    assert float(spo2_from_ratio(0.2)) == pytest.approx(float(spo2_from_ratio(R_SATURATED)))


def test_fast_pulse_is_not_halved():
    red, ir = channels(0.5, bpm=112)
    assert analyse_signal(red, ir, FS)["heart_rate_bpm"] == pytest.approx(112, abs=3)


def test_noise_and_dropouts_give_no_spo2():
    rng = np.random.default_rng(3)
    ir = 90000 + 400 * rng.standard_normal(9000)
    red = 50000 + 400 * rng.standard_normal(9000)
    result = analyse_signal(red, ir, FS)
    assert result["spo2_percent"] is None and result["quality"] == "insufficient"
    red, ir = channels(0.5)
    ir = ir.copy()
    ir[::700] = 1  # contact dropouts every 7 s
    result = analyse_signal(red, ir, FS)
    assert result["reject_reasons"].get("dropout_or_clipping", 0) > 0


def test_ir_only_keeps_pulse_without_spo2():
    _, ir = channels(0.5)
    result = analyse_signal(None, ir, FS)
    assert result["spo2_percent"] is None
    assert result["heart_rate_bpm"] == pytest.approx(78, abs=2)


def test_record_contract_timing_and_roundtrip(tmp_path):
    red, ir = channels(0.55)
    doc = document(red, ir)
    sample, available, basis = ppg_timing(doc)
    assert (basis, sample) == ("device_time", doc["time"])
    record, analysis = ppg_spo2_record(doc, source_path="x.json", source_sha256="a" * 64)
    assert record["create_time"] == sample and record["update_time"] >= sample + 89_000
    parsed = read_spo2_record(record)
    assert parsed["value"] == analysis["spo2_percent"] and parsed["ratio_r"] == analysis["ratio_r"]
    validate_contract(dict(CONTRACT))
    with pytest.raises(ValueError):
        validate_contract({**CONTRACT, "algorithm": "other"})
    # export into a Temp-isomorphic SpO2 tree
    folder = tmp_path / "产犊" / "PPG" / "2026-09-01" / f"{DEVICE}-22084-D3"
    folder.mkdir(parents=True)
    (folder / "a.json").write_text(json.dumps(doc), encoding="utf-8")
    noisy = document(90000 + 300 * np.random.default_rng(1).standard_normal(9000),
                     90000 + 300 * np.random.default_rng(2).standard_normal(9000), create=doc["create_time"] + 3_600_000)
    (folder / "b.json").write_text(json.dumps(noisy), encoding="utf-8")
    audit = []
    out = export_spo2_sources(sorted(folder.glob("*.json")), tmp_path / "ds", audit=audit)
    assert (out["samples"], out["rejected"], out["issues"]) == (1, 1, [])
    saved = list((tmp_path / "ds" / "SpO2").rglob("*.json"))
    assert len(saved) == 1 and saved[0].parent.name == f"{DEVICE}-22084-D3"
    assert {a["status"] for a in audit} == {"created", "rejected"}
    again = export_spo2_sources(sorted(folder.glob("*.json")), tmp_path / "ds")
    assert again["reused"] == 1 and again["created"] == 0


def _series(tmp_path, count=8, step_ms=3_960_000):
    folder = tmp_path / "PPG" / "2026-09-01" / f"{DEVICE}-22084-D3"
    folder.mkdir(parents=True)
    files = []
    for i in range(count):
        red, ir = channels(0.45 + 0.03 * i, seed=i)
        doc = document(red, ir, create=1_788_000_000_000 + i * step_ms)
        path = folder / f"{i:02d}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        files.append(path)
    return files


def test_plugin_contract_and_causality(tmp_path):
    module = load_feature("spo2")
    assert module.SPEC.modality == "ppg" and module.SPEC.primary in module.SPEC.columns
    files = _series(tmp_path)
    rows = validate_rows(module.SPEC, module.extract_series(files))
    assert rows and all(r["start_epoch_ms"] % 600_000 == 0 for r in rows)
    starts = [r["start_epoch_ms"] for r in rows]
    assert all(b - a == 600_000 for a, b in zip(starts, starts[1:]))  # gaps are kept as rows
    measurements = [measure_file(p) for p in files]
    first_sample = min(m["sample_epoch_ms"] for m in measurements)
    first_available = min(m["available_epoch_ms"] for m in measurements)
    last_available = max(m["available_epoch_ms"] for m in measurements)
    assert starts[0] <= first_sample < starts[0] + 600_000
    assert starts[-1] <= last_available < starts[-1] + 600_000
    # a value is never known before the server received the capture
    for row in rows:
        if row["start_epoch_ms"] < first_available:
            assert row["spo2_percent"] is None and row["perfusion_index_percent"] is None and row["coverage"] == 0
    assert any(r["spo2_percent"] is not None for r in rows)
    engine = SpO2Module(measurements)
    assert engine.evaluate(first_available - 1)["features"]["spo2_percent"] is None
    single = validate_rows(module.SPEC, module.extract(files[0]))
    assert single[0]["available_epoch_ms"] >= single[0]["end_epoch_ms"]


def test_unusable_capture_keeps_its_window(tmp_path):
    module = load_feature("spo2")
    files = _series(tmp_path, count=3)
    broken = json.loads(files[1].read_text(encoding="utf-8"))
    broken.pop("ir_data")  # waveform unusable, timing still known
    broken["data"] = broken.get("data", "")
    files[1].write_text(json.dumps(broken), encoding="utf-8")
    rows = module.extract_series(files)
    sample = broken["time"]
    window = (sample // 600_000) * 600_000
    assert any(r["start_epoch_ms"] == window for r in rows)


def test_attach_fills_fusion_rows(tmp_path):
    files = _series(tmp_path)
    records = [dict(cow_id="22084", device_id=DEVICE, field_mark="D3", raw=str(p)) for p in files]
    late = max(measure_file(p)["available_epoch_ms"] for p in files)
    rows = [dict(cow_id="22084", device_id=DEVICE, field_mark="D3", decision_epoch_ms=late),
            dict(cow_id="99999", device_id=DEVICE, field_mark="D3", decision_epoch_ms=late)]
    assert attach_spo2(rows, records) == []
    assert 80 < rows[0]["spo2_percent"] <= 100 and rows[0]["spo2_ratio_r"] is not None
    assert rows[1]["spo2_percent"] is None


def test_ppg_sheet_note_uses_algorithm():
    from cowmata_tailring.workspace.multi_sensor import oximetry_note
    from cowmata_tailring.workspace.sensor_records import parse_ppg_object
    red, ir = channels(0.5)
    note = oximetry_note([parse_ppg_object(document(red, ir), "x.json")])
    assert "血氧" in note and "脉率" in note


REAL = Path(r"F:\扬大_高邮牧场\产犊\PPG")


@pytest.mark.skipif(not REAL.is_dir(), reason="farm PPG not available")
def test_real_capture_smoke():
    path = next(REAL.rglob("*.json"))
    m = measure_file(path)
    assert m["available_epoch_ms"] >= m["sample_epoch_ms"]
    assert m["spo2_percent"] is None or 70 <= m["spo2_percent"] <= 100
