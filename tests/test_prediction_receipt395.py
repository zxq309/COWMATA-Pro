"""Receipt-delay regression: late packets cannot produce warnings before arrival."""

import numpy as np

from cowmata_tailring.algorithms import decision
from cowmata_tailring.algorithms.evidence import add_baselines

BASE = 1787130000000


def record(kind="motion"):
    return dict(
        raw=kind,
        asset_id=kind,
        cow_id="21314",
        device_id="546C50CA07E5",
        field_mark="D2",
        modality=kind,
    )


def feature(received, *, start=BASE, duration=600000):
    count = duration // 1000
    return dict(
        duration_ms=duration,
        seconds=np.arange(count),
        valid=np.ones(count, dtype=bool),
        dynamic=np.ones(count),
        segments=[(0, duration)],
        epoch_offset_ms=start,
        update_time_ms=received,
    )


def fusion(tmp_path, monkeypatch, received=BASE + 3900000, *, optical=None, temp_arrival=None):
    records = [record()]
    features = {"motion": feature(received)}
    if optical is not None:
        records.append(record("ppg"))
        features["ppg"] = feature(optical, start=BASE + 100000, duration=90000)
    # Feature extraction is independent; exercise the real evidence/fusion code with exact receipt times.
    monkeypatch.setattr(decision, "load_features", lambda rec, cache: features[rec["raw"]])
    temps = [
        dict(
            cow="21314",
            device="546C50CA07E5",
            mark="D2",
            time=BASE + 300000,
            value=38.5,
            available_at_ms=received if temp_arrival is None else temp_arrival,
            source="Temp/sample.json",
        )
    ]
    return decision.build_fusion(
        tmp_path,
        [],
        tmp_path / "out",
        tmp_path / "cache",
        input_index=dict(records=records, issues=[], fingerprint="receipt-test"),
        input_temperatures=temps,
    )


def test_whole_packet_waits_for_receipt_and_includes_its_temperature(tmp_path, monkeypatch):
    result = fusion(tmp_path, monkeypatch)
    row = result["rows"][0]
    assert row["decision_epoch_ms"] == BASE + 3900000
    assert row["start_epoch_ms"] == BASE and row["end_epoch_ms"] == BASE + 600000
    assert row["temperature_c"] == 38.5


def test_temperature_after_packet_decision_is_not_available(tmp_path, monkeypatch):
    row = fusion(tmp_path, monkeypatch, temp_arrival=BASE + 3900001)["rows"][0]
    assert row["temperature_c"] is None


def test_later_ppg_packet_does_not_leak_into_earlier_motion_prediction(tmp_path, monkeypatch):
    rows = fusion(tmp_path, monkeypatch, optical=BASE + 4000000)["rows"]
    assert len(rows) == 2
    motion = next(r for r in rows if r["modality"] == "motion")
    assert motion["ppg_coverage"] is None


def test_received_ppg_packet_can_join_motion(tmp_path, monkeypatch):
    rows = fusion(tmp_path, monkeypatch, optical=BASE + 3800000)["rows"]
    assert len(rows) == 1 and rows[0]["ppg_coverage"] == 0.15


def test_baseline_excludes_previous_samples_that_had_not_arrived():
    rows = [
        dict(
            cow_id="21314",
            asset_id=str(i),
            start_epoch_ms=BASE + i * 600000,
            end_epoch_ms=BASE + (i + 1) * 600000,
            decision_epoch_ms=BASE + 9000000,
            activity_index=2.0,
            temperature_c=38.0,
        )
        for i in range(6)
    ]
    rows.append(
        dict(
            cow_id="21314",
            asset_id="current",
            start_epoch_ms=BASE + 3600000,
            end_epoch_ms=BASE + 4200000,
            decision_epoch_ms=BASE + 4300000,
            activity_index=1.0,
            temperature_c=39.0,
        )
    )
    add_baselines(rows)
    assert rows[-1]["baseline_windows"] == 0
    assert rows[-1]["temperature_c_baseline"] is None


def test_training_and_prediction_require_matching_receipt_policy(tmp_path):
    import csv
    import json
    from datetime import datetime, timedelta, timezone

    import pytest

    birth = datetime(2026, 8, 12, 12, tzinfo=timezone(timedelta(hours=8)))
    ledger = tmp_path / "birth.csv"
    with ledger.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["牛号", "生产日期", "牛场登记生产时间"])
        writer.writeheader()
        writer.writerows(
            dict(牛号=str(cow), 生产日期="2026-08-12", 牛场登记生产时间="12:00:00")
            for cow in range(20001, 20007)
        )
    rows = []
    for cow in range(20001, 20007):
        for hours in (6, 12, 36, 48):
            cutoff = (birth - timedelta(hours=hours)).timestamp() * 1000
            rows.append(
                dict(
                    cow_id=str(cow),
                    source="fixture",
                    decision_epoch_ms=cutoff,
                    end_epoch_ms=cutoff,
                    activity_index=hours / 60,
                    temperature_c=38 + hours / 60,
                    motion_coverage=1,
                )
            )
    evidence = dict(
        rows=rows,
        index_fingerprint="fixture",
        behavior_models=[],
        timing_policy="server-receipt-v1",
    )
    trained = decision.train_decision(evidence, ledger, tmp_path / "model", "decision_tree")
    assert trained.get("timing_policy") == "server-receipt-v1"
    assert (
        len(
            decision.predict_decision(evidence, tmp_path / "model", tmp_path / "prediction")["rows"]
        )
        == 24
    )
    with pytest.raises(ValueError, match="时间.*重新训练"):
        decision.predict_decision(
            {**evidence, "timing_policy": None}, tmp_path / "model", tmp_path / "old-input"
        )
    manifest = tmp_path / "model/decision.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc.pop("timing_policy")
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="时间.*重新训练"):
        decision.predict_decision(evidence, tmp_path / "model", tmp_path / "old-model")
