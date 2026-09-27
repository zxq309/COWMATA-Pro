"""躺卧占比 decision feature (lying-occupancy-2): contract, state layer, wear detection, model checks."""
import hashlib
import json

import numpy as np
import pytest

from cowmata_engine.features import load_feature, validate_rows
from cowmata_engine.features import lying_ratio as F
from cowmata_tailring.algorithms import lying_occupancy as L

WIN = 600_000
E0 = 1_787_900_400_000  # multiple of 10 min
STAND = np.array([0.0, -0.93, 0.37])
LIE = np.array([0.45, -0.89, 0.0])


def _summary(orientation, seconds, *, gyro_sd=6.0, wag=True, seed=0, dyn=None):
    """Per-second summary with a given gravity direction per second and a living-tail motion.

    ``dyn`` is the dynamic acceleration per second (walking/feeding is higher while standing).
    """
    rng = np.random.default_rng(seed)
    u = np.asarray(orientation, float)
    if u.ndim == 1:
        u = np.tile(u, (seconds, 1))
    u = u / np.linalg.norm(u, axis=1, keepdims=True) + rng.normal(0, 0.004, (seconds, 3))
    gsd = np.abs(rng.normal(gyro_sd, 1.0, (seconds, 3))) if wag else np.full((seconds, 3), 0.05)
    d = np.full(seconds, 0.02) if dyn is None else np.asarray(dyn, float)
    return dict(coverage=np.ones(seconds), acc_mean=u, acc_sd=np.tile(d[:, None] / np.sqrt(3), (1, 3)),
                gyr_mean=rng.normal(0, 1, (seconds, 3)), gyr_sd=gsd)


def _stand_lie(n, change_s):
    orient = np.tile(STAND, (n, 1))
    orient[change_s:] = LIE
    dyn = np.where(np.arange(n) < change_s, 0.08, 0.01)
    return orient, dyn


def _logit(n_features, weights, intercept=0.0, classes=(0, 1)):
    w = np.atleast_2d(weights)
    return dict(kind="logit", median=[0.0] * n_features, mean=[0.0] * n_features, scale=[1.0] * n_features,
                coef=w.tolist(), intercept=np.atleast_1d(intercept).tolist(), classes=list(classes))


def _models():
    i = L.POSTURE_FEATURES.index("ang_ref_30")
    w = np.zeros(len(L.POSTURE_FEATURES))
    w[i] = 0.4  # lying when the tail ring is >~ 12 deg away from the standing reference
    posture = _logit(len(L.POSTURE_FEATURES), w, -5.0)
    k = len(L.TRANSITION_FEATURES)
    wt = np.zeros((3, k))
    wt[1, L.TRANSITION_FEATURES.index("dz")] = 12.0
    wt[2, L.TRANSITION_FEATURES.index("dz")] = -12.0
    transition = _logit(k, wt, [2.0, -1.0, -1.0], classes=(0, 1, 2))
    return dict(posture=posture, transition=transition, hmm=dict(L.DEFAULT_HMM))


def test_plugin_contract():
    module = load_feature("lying_ratio")
    assert module.SPEC.key == "lying_ratio" and module.SPEC.modality == "motion"
    assert module.SPEC.primary == "lying_ratio" and module.SPEC.lookahead_ms == L.LOOKAHEAD_MS
    assert set(module.SPEC.column_titles) == set(module.SPEC.columns)


def test_summarize_motion_bins_by_second():
    t = np.arange(0, 3000, 20.0)
    acc = np.tile([0.0, -0.9, 0.4], (len(t), 1))
    gyro = np.zeros((len(t), 3))
    gyro[:, 0] = np.where(t < 1000, 10.0, 0.0)
    s = L.summarize_motion(t, acc, gyro)
    assert len(s["coverage"]) == 3 and np.allclose(s["coverage"], 1.0)
    assert np.allclose(s["acc_mean"][1], [0.0, -0.9, 0.4])
    assert s["gyr_mean"][0, 0] == pytest.approx(10.0) and s["gyr_mean"][2, 0] == 0


def test_stand_then_lie_gives_whole_window_ratios():
    n = 4 * 3600
    orient, dyn = _stand_lie(n, 2 * 3600)
    rows, detail = L.analyse_series([(E0, _summary(orient, n, dyn=dyn))], _models())
    validate_rows(F.SPEC, [dict(r) for r in rows])
    ratios = [r["lying_ratio"] for r in rows]
    assert all(r is not None for r in ratios)
    assert np.mean(ratios[2:10]) < 0.1 and np.mean(ratios[14:22]) > 0.9
    assert sum(r["lying_down_count"] for r in rows) == 1
    assert all(r["available_epoch_ms"] == r["end_epoch_ms"] + L.LOOKAHEAD_MS for r in rows)
    assert all(r["start_epoch_ms"] % WIN == 0 for r in rows)


def test_state_is_causal_within_lookahead():
    n = 3 * 3600
    orient, dyn = _stand_lie(n, 2 * 3600)
    models = _models()
    full, _ = L.analyse_series([(E0, _summary(orient, n, dyn=dyn))], models)
    # Truncate the series 15 min after the end of the first hour: its windows must not change.
    cut = 3600 + L.LOOKAHEAD_MS // 1000
    part, _ = L.analyse_series([(E0, _summary(orient[:cut], cut, dyn=dyn[:cut]))], models)
    a = {r["start_epoch_ms"]: r["lying_ratio"] for r in full}
    b = {r["start_epoch_ms"]: r["lying_ratio"] for r in part}
    for start in range(E0, E0 + 3600 * 1000 - WIN + 1, WIN):
        assert a[start] == pytest.approx(b[start], abs=1e-9)


def test_detached_ring_is_not_reported_as_lying():
    n = 3 * 3600
    worn = _summary(STAND, n)
    off = _summary([0.9, 0.1, -0.4], 2 * 3600, wag=False)  # fell off: gravity off the tail axis, no motion
    rows, detail = L.analyse_series([(E0, worn), (E0 + n * 1000, off)], _models())
    late = [r for r in rows if r["start_epoch_ms"] >= E0 + (n + 1800) * 1000]
    assert late and all(r["lying_ratio"] is None and r["coverage"] < 0.2 for r in late)


def test_hard_path_merges_short_bouts():
    post = np.r_[np.zeros(300), np.ones(20), np.zeros(300), np.ones(400)]
    s = L.hard_path(post, np.ones(len(post), bool), 60)
    assert s[300:320].sum() == 0 and s[-400:].all()


def test_model_folder_checksum(tmp_path):
    models = _models()
    names = {"posture.json": models["posture"], "transition.json": models["transition"], "hmm.json": models["hmm"]}
    sha = {}
    for name, doc in names.items():
        data = json.dumps(doc).encode("utf-8")
        (tmp_path / name).write_bytes(data)
        sha[name] = hashlib.sha256(data).hexdigest()
    (tmp_path / F.PARAMS_FILE).write_text(json.dumps(dict(schema=F.PARAMS_SCHEMA, algorithm=L.ALGORITHM, sha256=sha)),
                                          encoding="utf-8")
    loaded = F.load_model(tmp_path)
    assert set(loaded) == {"posture", "transition", "hmm"}
    rows = F.extract_summaries([(E0, _summary(STAND, 1800))], ["a_raw.json"], model_dir=tmp_path)
    validate_rows(F.SPEC, rows)
    assert rows and all(r["source"] == "a_raw.json" for r in rows)
    bad = tmp_path / "bad"
    bad.mkdir()
    for name in (*names, F.PARAMS_FILE):
        (bad / name).write_bytes((tmp_path / name).read_bytes())
    (bad / "hmm.json").write_text(json.dumps(dict(models["hmm"], kappa=9)), encoding="utf-8")
    with pytest.raises(ValueError):
        F.load_model(bad)
    with pytest.raises(FileNotFoundError):
        F.load_model(tmp_path / "missing")
