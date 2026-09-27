"""gse-1 angular-velocity spectral entropy plug-in (cowmata-decision-feature-1)."""
from __future__ import annotations

import glob

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_engine.features import gyro_spectral_entropy as g

FS = 50.0
MINUTE = 60_000


def _block(signal, seconds=60):
    t = np.arange(int(seconds * FS)) / FS
    return signal(t)


def test_registered_and_contract():
    module = load_feature("gyro_spectral_entropy")
    assert module.SPEC.key == "gyro_spectral_entropy"
    assert module.SPEC.modality == "motion"
    assert module.SPEC.primary in module.SPEC.columns
    assert hasattr(module, "extract_series")


def test_white_noise_is_flat_and_sine_is_concentrated():
    rng = np.random.default_rng(1)
    noise = g.spectrum_features(rng.normal(0, 5, (3000, 3)), FS)
    slow = g.spectrum_features(_block(lambda t: np.column_stack([30 * np.sin(2 * np.pi * 0.3 * t)] * 3)), FS)
    assert noise["entropy"] > 0.95
    assert slow["entropy"] < 0.6
    assert slow["low"] > 0.9 and slow["high"] < 0.05
    assert abs(slow["peak_hz"] - 0.3) <= 0.1
    assert abs(noise["low"] + noise["mid"] + noise["high"] - 1) < 1e-9


def test_entropy_is_invariant_to_ring_rotation():
    rng = np.random.default_rng(2)
    t = np.arange(3000) / FS
    gyro = np.column_stack([20 * np.sin(2 * np.pi * 1.2 * t), 5 * np.sin(2 * np.pi * 0.2 * t), rng.normal(0, 2, 3000)])
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    a, b = g.spectrum_features(gyro, FS), g.spectrum_features(gyro @ q.T, FS)
    for key in ("entropy", "low", "mid", "high", "power"):
        assert a[key] == pytest.approx(b[key], rel=1e-9, abs=1e-12)


def test_short_or_bad_blocks_are_missing_not_zero():
    assert g.spectrum_features(np.zeros((100, 3)), FS) is None
    assert g.spectrum_features(np.zeros((3000, 3)), FS) is None  # no power at all


def _minutes(start_ms, count, entropy=0.9, power=10.0, step=1):
    rows = [[start_ms + i * step * MINUTE, entropy, power, 0.1, 0.5, 0.4, 1.0, 50.0] for i in range(count)]
    return np.asarray(rows, dtype=float)


def test_windows_are_absolute_causal_and_validated():
    start = 1_787_936_400_000 + 3 * MINUTE  # not on a 10 min boundary
    minutes = _minutes(start, 12)
    received = np.full(len(minutes), start + 40 * MINUTE)
    rows = validate_rows(g.SPEC, g.windows_from_minutes(minutes, received))
    assert [r["start_epoch_ms"] % 600_000 for r in rows] == [0, 0]
    assert rows[0]["coverage"] == pytest.approx(0.7)
    assert rows[1]["coverage"] == pytest.approx(0.5)
    assert all(r["available_epoch_ms"] >= r["end_epoch_ms"] for r in rows)
    assert rows[0]["available_epoch_ms"] == start + 40 * MINUTE
    assert rows[0]["gyro_spectral_entropy"] == pytest.approx(0.9)
    assert rows[0]["gyro_valid_minutes"] == 7


def test_still_tail_keeps_shape_columns_missing():
    rows = g.windows_from_minutes(_minutes(1_787_936_400_000, 10, entropy=0.99, power=0.003))
    assert rows[0]["coverage"] == 1.0
    shape = ("gyro_spectral_entropy", "gyro_band_low_frac", "gyro_band_high_frac")
    assert all(rows[0][c] is None for c in shape)
    assert rows[0]["gyro_power_log10"] == pytest.approx(np.log10(0.003 + 1e-6))  # stillness itself is informative
    assert rows[0]["gyro_active_fraction"] == 0.0
    validate_rows(g.SPEC, rows)


def test_duplicate_minutes_from_overlapping_uploads_count_once():
    m = _minutes(1_787_936_400_000, 5)
    rows = g.windows_from_minutes(np.vstack([m, m]))
    assert rows[0]["gyro_valid_minutes"] == 5


def test_noise_minutes_are_excluded_and_bands_power_weighted():
    t = 1_787_936_400_000
    quiet = [[t + i * MINUTE, 0.99, 0.01, 0.0, 0.0, 1.0, 4.0, 50.0] for i in range(4)]
    busy = [[t + (4 + i) * MINUTE, 0.60, 100.0 * (i + 1), 1.0, 0.0, 0.0, 0.3, 50.0] for i in range(3)]
    row = g.windows_from_minutes(np.asarray(quiet + busy))[0]
    assert row["gyro_spectral_entropy"] == pytest.approx(0.60)
    assert row["gyro_band_low_frac"] == pytest.approx(1.0)
    assert row["gyro_band_high_frac"] == pytest.approx(0.0)
    assert row["gyro_active_fraction"] == pytest.approx(3 / 7)


def test_exported_columns_and_derivations_are_the_validated_set():
    assert g.SPEC.columns == ("gyro_spectral_entropy", "gyro_band_low_frac", "gyro_band_high_frac", "gyro_power_log10")
    assert not set(g.DIAGNOSTICS) & set(g.SPEC.columns)
    from cowmata_engine.decision.dataset import DERIVATIONS
    assert set(g.SPEC.derivations) <= set(DERIVATIONS)
    assert "z72" in g.SPEC.derivations  # expert rule reads primary@z72
    assert "d24" not in g.SPEC.derivations


REAL = sorted(glob.glob(r"F:\扬大_高邮牧场\产犊\Motion\2026-08-29\*\*.json"))[:2]


@pytest.mark.skipif(not REAL, reason="扬大 raw data not mounted")
def test_real_motion_series():
    rows = validate_rows(g.SPEC, g.extract_series(REAL))
    assert rows and all(0 <= r["coverage"] <= 1 for r in rows)
    values = [r["gyro_spectral_entropy"] for r in rows if r["gyro_spectral_entropy"] is not None]
    assert values and all(0 < v <= 1 for v in values)
    starts = [r["start_epoch_ms"] for r in rows]
    assert starts == sorted(set(starts))
