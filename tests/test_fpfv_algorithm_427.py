"""FETAL_PART_FIRST_VISIBLE tail-hold / stage-II onset algorithm (fpfv)."""
import numpy as np
import pytest

from cowmata_tailring.algorithms import fpfv


def synthetic_parts(minutes=240, onset_min=180, calf_min=195, seed=3):
    """One device, 50 Hz; tail hangs (gravity on -Y) then is held lifted between onset and calf."""
    rng = np.random.default_rng(seed)
    parts = []
    for rec in range(0, minutes, 60):
        n = 60 * 60 * 50
        t = np.arange(n) * 20.0
        minute = rec + t / 60000
        elev = np.where((minute >= onset_min) & (minute < calf_min), np.radians(60), np.radians(8))
        elev = elev + np.radians(3) * rng.standard_normal(n)
        acc = np.column_stack([np.zeros(n), -np.cos(elev), np.sin(elev)]) + .02 * rng.standard_normal((n, 3))
        gyr = 3 * rng.standard_normal((n, 3))
        s = fpfv.second_summary(t, acc, gyr)
        s.update(epoch0_ms=1.7e12 + rec * 60000.0, temp_c=np.full(60, 37.8), temp_ms=np.arange(60) * 60000.0)
        parts.append(s)
    return parts


def test_tail_elevation_is_invariant_to_ring_rotation():
    from cowmata_tailring.algorithms.fpfv_features import base_signals
    angle = np.radians(40)
    rows = []
    for spin in np.radians([0, 90, 200]):
        g = np.array([np.sin(angle) * np.cos(spin), -np.cos(angle), np.sin(angle) * np.sin(spin)])
        rows.append(g)
    s = dict(acc_mean=np.array(rows), acc_sd=np.zeros((3, 3)), gyr_sd=np.ones((3, 3)), jerk=np.zeros(3),
             valid=np.ones(3, bool), epoch_s=np.arange(3.), temp_ms=np.zeros(0), temp_c=np.zeros(0))
    assert np.allclose(base_signals(s)["elev"], 40, atol=1e-6)


def test_continuous_series_keeps_gaps_and_order():
    parts = synthetic_parts(minutes=120)
    series = fpfv.continuous(parts[::-1])
    assert len(series["epoch_s"]) == 7200
    assert np.all(np.diff(series["epoch_s"]) == 1)
    assert series["valid"].mean() > .99
    gap = dict(parts[1], epoch0_ms=parts[1]["epoch0_ms"] + 600000)
    s2 = fpfv.continuous([parts[0], gap])
    assert not s2["valid"][3600:4200].any()


def test_numeric_gbdt_export_matches_sklearn():
    from sklearn.ensemble import HistGradientBoostingClassifier
    rng = np.random.default_rng(0)
    X = rng.standard_normal((2000, 5))
    X[rng.random(X.shape) < .05] = np.nan
    y = (np.nan_to_num(X[:, 0]) + .5 * np.nan_to_num(X[:, 1]) > .3).astype(int)
    m = HistGradientBoostingClassifier(max_iter=30, max_leaf_nodes=7, early_stopping=False).fit(X, y)
    payload = fpfv.export_hgb(m, list("abcde"))
    assert np.allclose(fpfv.predict_gbdt(payload, X), m.predict_proba(X)[:, 1], atol=1e-9)
    with pytest.raises(ValueError):
        fpfv.predict_gbdt(payload, X[:, :4])


def test_decoder_returns_rising_edge_one_per_window():
    t = np.arange(0, 6 * 3600e3, 10e3)
    p = np.zeros(len(t))
    on = (t >= 3600e3) & (t < 3600e3 + 20 * 60e3)
    p[on] = .9
    p[(t >= 3 * 3600e3) & (t < 3 * 3600e3 + 5 * 60e3)] = .6       # weaker second episode, same 12 h
    events = fpfv.decode(t, p, threshold=.5, onset_frac=.5)
    assert len(events) == 1
    assert abs(events[0]["point_ms"] - 3600e3) <= 10e3


def test_detect_series_marks_point_candidate_for_video_review():
    parts = synthetic_parts()
    series = fpfv.continuous(parts)
    X, t, names = fpfv.feature_matrix(series)
    assert "temp" not in names and "base_elev_24h" not in names
    assert "up_rel_rate_3600" in names and "fut_hold_rel_frac_300" in names
    # a transparent single-feature model: hold fraction relative to own carriage (future 5 min)
    j = names.index("fut_hold_rel_frac_300")
    model = dict(schema=fpfv.MODEL_SCHEMA, features=names, baseline=-4.0, code=fpfv.CODE,
                 trees=[dict(feature=[j, 0, 0], threshold=[.5, 0, 0], nan_left=[1, 0, 0], left=[1, 0, 0],
                             right=[2, 0, 0], leaf=[0, 1, 1], value=[0, 0, 8.0])], decoder=dict(threshold=.5, onset_frac=.5))
    events, trace = fpfv.detect_series(series, model)
    assert len(events) == 1
    e = events[0]
    onset = parts[0]["epoch0_ms"] + 180 * 60000
    assert abs(e["point_epoch_ms"] - onset) <= 5 * 60000
    assert e["requires_video_confirmation"] is True and e["time_semantics"] == "approximate_point"
