"""温度 decision feature (cowmata-decision-feature-1): contract, arithmetic and failure modes."""
import base64
import json
from pathlib import Path

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_engine.features import temperature as T

WIN = 600_000
E0 = 1_787_900_400_000  # multiple of 10 min
REAL = Path(r"F:\科牧特_数据集\COWMATA_CalvingPred_Dataset\CalfFullyExpelled\Motion\Raw"
            r"\0C3D5EA22DD3-23291-P10_2026-08-27_10-47-31_raw.json")


def _packet(tmp_path, name, *, start_ms, minutes, temps, lying=False, received=None, first_elapsed=15):
    """Synthetic V2 <I9h> 50 Hz packet with one int16 temperature bucket per minute."""
    t = np.arange(0, minutes * 60_000, 20, dtype=np.int64)
    frames = np.zeros(len(t), dtype=[("elapsed_ms", "<u4"), ("values", "<i2", (9,))])
    frames["elapsed_ms"] = t + first_elapsed
    if lying:  # tail rolled onto its side: gravity on X
        frames["values"][:, 0] = 4096
    else:  # tail hanging: gravity on Z (roll 0 deg)
        frames["values"][:, 2] = 4096
    raw = np.round(np.asarray(temps, float) * 100).astype("<i2")
    doc = dict(device="0C3D5EA22DD2", version=2, uid=name, create_time=int(start_ms - first_elapsed),
               update_time=int(received if received is not None else start_ms + minutes * 60_000 + 300_000),
               imu=base64.b64encode(frames.tobytes()).decode("ascii"),
               temperature=base64.b64encode(raw.tobytes()).decode("ascii"))
    path = tmp_path / f"0C3D5EA22DD2-22065-H8_{name}_raw.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_plugin_contract():
    module = load_feature("temperature")
    spec = module.SPEC
    assert spec.key == "temperature" and spec.modality == "motion" and spec.primary == "temp_median_c"
    assert spec.primary in spec.columns and spec.lookahead_ms == 0
    assert set(spec.column_titles) == set(spec.columns)
    assert "0.37" in spec.expected_change and "日节律" in spec.expected_change
    assert hasattr(module, "extract_series")


def test_window_statistics_and_alignment(tmp_path):
    temps = [38.0 + 0.1 * i for i in range(20)]
    p = _packet(tmp_path, "a", start_ms=E0, minutes=20, temps=temps, received=E0 + 2 * WIN + 1_800_000)
    rows = validate_rows(T.SPEC, T.extract(p))
    assert [r["start_epoch_ms"] for r in rows] == [E0, E0 + WIN]
    first, second = rows
    assert first["temp_median_c"] == pytest.approx(np.median(temps[:10]))
    assert first["temp_max_c"] == pytest.approx(38.9)
    assert second["temp_max_c"] == pytest.approx(39.9)
    assert first["temp_standing_median_c"] == pytest.approx(first["temp_median_c"])
    assert first["coverage"] == pytest.approx(1.0) and first["on_cow_fraction"] == 1.0
    assert all(r["available_epoch_ms"] == E0 + 2 * WIN + 1_800_000 for r in rows)


def test_off_cow_minutes_are_excluded_not_zeroed(tmp_path):
    temps = [25.5] * 7 + [38.2] * 3 + [38.4] * 10
    rows = T.extract(_packet(tmp_path, "b", start_ms=E0, minutes=20, temps=temps))
    detached, worn = rows
    assert detached["on_cow_fraction"] == pytest.approx(0.3)
    assert detached["temp_median_c"] is None and detached["temp_max_c"] is None  # only 3 on-cow minutes
    assert detached["coverage"] == pytest.approx(1.0)
    assert worn["temp_median_c"] == pytest.approx(38.4)


def test_lying_minutes_have_no_standing_median(tmp_path):
    rows = T.extract(_packet(tmp_path, "c", start_ms=E0, minutes=10, temps=[38.6] * 10, lying=True))
    assert rows[0]["temp_median_c"] == pytest.approx(38.6)
    assert rows[0]["temp_standing_median_c"] is None


def test_windows_split_across_packets_are_merged_and_duplicates_counted_once(tmp_path):
    temps = [38.0 + 0.05 * i for i in range(20)]
    whole = _packet(tmp_path, "w", start_ms=E0, minutes=20, temps=temps)
    first = _packet(tmp_path, "p1", start_ms=E0, minutes=15, temps=temps[:15], received=E0 + 20 * 60_000)
    second = _packet(tmp_path, "p2", start_ms=E0 + 15 * 60_000, minutes=5, temps=temps[15:], received=E0 + 40 * 60_000)
    merged = T.extract_series([first, second, second])
    alone = T.extract(first)
    ref = T.extract(whole)
    assert alone[1]["coverage"] == pytest.approx(0.5)
    assert merged[1]["coverage"] == pytest.approx(1.0)
    assert merged[1]["temp_median_c"] == pytest.approx(ref[1]["temp_median_c"])
    assert merged[1]["available_epoch_ms"] == E0 + 40 * 60_000
    assert merged[0]["available_epoch_ms"] == E0 + 20 * 60_000


def test_standalone_temp_json(tmp_path):
    docs = []
    for i in range(6):
        doc = dict(configs=None, cow_id="", create_time=E0 + i * 60_000 + 30_000, data=38.1 + 0.01 * i,
                   device="0C3D5EA22DD2", uid=f"t{i}", update_time=E0 + WIN + 120_000)
        path = tmp_path / f"0C3D5EA22DD2-22065-H8_{i}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        docs.append(path)
    row = validate_rows(T.SPEC, T.extract_series(docs))[0]
    assert row["temp_median_c"] == pytest.approx(38.125)
    assert row["temp_standing_median_c"] is None
    assert row["available_epoch_ms"] == E0 + WIN + 120_000


def test_motion_without_temperature_keeps_windows_and_bad_files_are_skipped(tmp_path):
    p = _packet(tmp_path, "n", start_ms=E0, minutes=20, temps=[38.0] * 20)
    doc = json.loads(p.read_text(encoding="utf-8"))
    del doc["temperature"]
    p.write_text(json.dumps(doc), encoding="utf-8")
    bad = tmp_path / "0C3D5EA22DD2-22065-H8_bad_raw.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _packet(tmp_path, "g", start_ms=E0 + 2 * WIN, minutes=10, temps=[38.3] * 10)
    issues = []
    rows = validate_rows(T.SPEC, T.extract_series([p, bad, good], issues=issues))
    assert [r["start_epoch_ms"] for r in rows] == [E0, E0 + WIN, E0 + 2 * WIN]
    assert rows[0]["coverage"] == 0.0 and all(rows[0][c] is None for c in T.SPEC.columns)
    assert rows[0]["available_epoch_ms"] >= rows[0]["end_epoch_ms"]
    assert rows[2]["temp_median_c"] == pytest.approx(38.3)
    assert len(issues) == 1 and issues[0]["path"].endswith("_bad_raw.json")


@pytest.mark.skipif(not REAL.is_file(), reason="CalvingPred dataset not mounted")
def test_real_file_matches_contract_extractor():
    from cowmata_tailring.temperature import motion_temperature_records

    obj = json.loads(REAL.read_text(encoding="utf-8-sig"))
    records = motion_temperature_records(obj, source_sha256="x")
    _, minutes = T.packet_minutes(REAL)
    assert [(m[0], m[1]) for m in minutes] == [(r["create_time"], r["data"]) for r in records]
    rows = validate_rows(T.SPEC, T.extract(REAL))
    on = [r for r in rows if r["temp_median_c"] is not None]
    assert on and all(34 <= r["temp_median_c"] <= 41.5 for r in on)