"""活动量 decision feature (cowmata-decision-feature-1): contract, arithmetic and failure modes."""
import base64
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_engine.features import activity as A

WIN = 600_000
E0 = 1_787_900_400_000  # multiple of 10 min
ROOT = Path(__file__).resolve().parents[1]


def _packet(tmp_path, name, *, start_ms, seconds, amp_g=0.0, freq=2.0, gap=None, motion=None, received=None,
            first_elapsed=15):
    """Synthetic V2 <I9h> tail-ring packet at 50 Hz; ``gap`` = (from_s, to_s) without frames."""
    t = np.arange(0, seconds * 1000, 20, dtype=np.int64)
    if gap:
        t = t[(t < gap[0] * 1000) | (t >= gap[1] * 1000)]
    rng = np.random.default_rng(len(name))
    z = 4096 + amp_g * 4096 * np.sin(2 * np.pi * freq * t / 1000) + rng.normal(0, 4, len(t))
    frames = np.zeros(len(t), dtype=[("elapsed_ms", "<u4"), ("values", "<i2", (9,))])
    frames["elapsed_ms"] = t + first_elapsed
    frames["values"][:, 0] = rng.normal(0, 4, len(t)).round()
    frames["values"][:, 2] = z.round()
    doc = dict(device="0C3D5EA22DD2", version=2, uid=name, create_time=int(start_ms - first_elapsed),
               update_time=int(received if received is not None else start_ms + seconds * 1000 + 300_000),
               imu=base64.b64encode(frames.tobytes()).decode("ascii"))
    if motion is not None:
        doc["motion"] = base64.b64encode(np.asarray(motion, "<u2").tobytes()).decode("ascii")
    path = tmp_path / f"0C3D5EA22DD2-22065-H8_{name}_raw.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_plugin_contract():
    module = load_feature("activity")
    spec = module.SPEC
    assert spec.key == "activity" and spec.modality == "motion" and spec.primary == "vedba_mean_g"
    assert spec.lookahead_ms == 0 and spec.primary in spec.columns
    assert set(spec.column_titles) == set(spec.columns)
    assert "+86%" in spec.expected_change


def test_still_versus_moving_windows(tmp_path):
    still = _packet(tmp_path, "a", start_ms=E0, seconds=600)
    moving = _packet(tmp_path, "b", start_ms=E0 + WIN, seconds=600, amp_g=0.4, received=E0 + 2 * WIN + 1_800_000)
    rows = A.extract_series([still, moving])
    validate_rows(A.SPEC, rows)
    assert [r["start_epoch_ms"] for r in rows] == [E0, E0 + WIN]
    calm, active = rows
    assert calm["vedba_mean_g"] < 0.01 and calm["active_frac"] == 0 and calm["high_frac"] == 0
    assert calm["bouts_per_h"] == 0
    # 0.4 g sine: VeDBA ~ 0.4*2/pi on the 2 s running-mean gravity
    assert 0.2 < active["vedba_mean_g"] < 0.3
    assert active["active_frac"] > 0.95 and active["high_frac"] > 0.9
    assert active["activity_index"] > calm["activity_index"]
    assert active["coverage"] > 0.99 and calm["coverage"] > 0.98
    # availability = server receipt of the packet completing the window, never earlier than the end
    assert active["available_epoch_ms"] == E0 + 2 * WIN + 1_800_000
    assert calm["available_epoch_ms"] >= calm["end_epoch_ms"]
    assert calm["device_motion_per_min"] is None  # no motion bucket -> None, not 0


def test_windows_split_across_packets_are_merged(tmp_path):
    whole = _packet(tmp_path, "w", start_ms=E0, seconds=1200, amp_g=0.1)
    first = _packet(tmp_path, "p1", start_ms=E0, seconds=900, amp_g=0.1)
    second = _packet(tmp_path, "p2", start_ms=E0 + 900_000, seconds=300, amp_g=0.1)
    merged = A.extract_series([first, second])
    alone = A.extract(first)
    assert [r["start_epoch_ms"] for r in merged] == [E0, E0 + WIN]
    assert alone[1]["coverage"] == pytest.approx(0.5, abs=0.01)
    assert merged[1]["coverage"] > 0.98
    ref = A.extract(whole)
    assert merged[1]["vedba_mean_g"] == pytest.approx(ref[1]["vedba_mean_g"], rel=0.02)


def test_gap_is_not_interpolated_and_short_windows_are_none(tmp_path):
    p = _packet(tmp_path, "g", start_ms=E0, seconds=1200, amp_g=0.3, gap=(300, 1170))
    rows = A.extract(p)
    assert rows[0]["coverage"] == pytest.approx(0.5, abs=0.01)
    last = rows[1]
    assert last["coverage"] < 0.06
    assert all(last[c] is None for c in A.SPEC.columns)  # < 60 valid seconds


def test_device_motion_bucket(tmp_path):
    p = _packet(tmp_path, "m", start_ms=E0, seconds=1200, motion=[10] * 10 + [30] * 10)
    rows = A.extract(p)
    assert rows[0]["device_motion_per_min"] == pytest.approx(10)
    assert rows[1]["device_motion_per_min"] == pytest.approx(30)


def test_vedba_matches_first_version_activity_engine(tmp_path):
    sys.path.insert(0, str(ROOT / "assets" / "dataset_recipes"))
    from cowmata_activity_aux.features import Config, packet_minutes
    from cowmata_activity_aux.protocol import Context, decode_packet

    p = _packet(tmp_path, "e", start_ms=E0 + 12_345, seconds=900, amp_g=0.25, freq=1.3)
    packet = decode_packet(json.loads(p.read_text(encoding="utf-8")), Context("22065", "0C3D5EA22DD2", "b", 0))
    engine, _ = packet_minutes(packet, Config())
    _, minutes = A.minute_accumulators(p)
    for end_ms, total, count, _ in engine:
        mine = minutes[end_ms - 60_000]
        assert mine["vedba_n"] == count
        # parse_motion_object scales in float32; the engine divides raw counts in float64
        assert mine["vedba_sum"] == pytest.approx(total, rel=1e-6)


def test_real_record_if_dataset_present():
    folder = Path(r"F:\科牧特_数据集\COWMATA_CalvingPred_Dataset\CalfFullyExpelled\Motion\Raw")
    files = sorted(folder.glob("*_raw.json"))[:1] if folder.is_dir() else []
    if not files:
        pytest.skip("CalvingPred dataset not available")
    rows = A.extract(files[0])
    validate_rows(A.SPEC, rows)
    assert len(rows) >= 6 and all(r["start_epoch_ms"] % WIN == 0 for r in rows)
