"""Tail-ring lying-down detector: physics law, calibration, posture labels and app integration."""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from cowmata_tailring.algorithms import liedown as LD
from cowmata_tailring.algorithms.models import export_forest, predict_forest

STAND = np.array([-0.08, -0.92, 0.37])
LIE = np.array([0.60, -0.80, -0.05])


def _unit(v):
    return v / np.linalg.norm(v)


def synthetic(transitions, minutes=20, hz=50, seed=0):
    """transitions: list of (time_s, from_vec, to_vec, duration_s). Standing sways, lying is quiet."""
    rng = np.random.default_rng(seed)
    n = minutes * 60 * hz
    t = np.arange(n) * 1000.0 / hz
    sec = t / 1000
    grav = np.tile(_unit(transitions[0][1]), (n, 1)) if transitions else np.tile(_unit(STAND), (n, 1))
    standing = np.ones(n, bool)
    gyro = np.zeros((n, 3))
    for at, a, b, dur in transitions:
        m = (sec >= at) & (sec < at + dur)
        w = np.clip((sec[m] - at) / dur, 0, 1)[:, None]
        grav[m] = _unit(a) * (1 - w) + _unit(b) * w
        grav[sec >= at + dur] = _unit(b)
        gyro[m] += rng.normal(0, 35, (m.sum(), 3))
        standing[sec >= at + dur] = np.allclose(_unit(b), _unit(STAND))
    grav /= np.linalg.norm(grav, axis=1, keepdims=True)
    sway = np.where(standing, 1.0, 0.15)[:, None]
    acc = grav + rng.normal(0, 0.02, (n, 3)) * sway
    gyro += rng.normal(0, 3, (n, 3)) * sway
    return t, acc, gyro


def test_law_only_detects_lying_down_and_ignores_standing_up():
    t, acc, gyro = synthetic([(300, STAND, LIE, 6), (900, LIE, STAND, 5)])
    events = LD.detect(t, acc, gyro, model=None)
    assert len(events) == 1
    e = events[0]
    assert e["code"] == "LYING_DOWN"
    assert 296_000 <= e["point_ms"] <= 310_000
    assert e["start_ms"] <= e["point_ms"] <= e["end_ms"]
    assert e["evidence"]["z_change_g"] < -0.2
    assert e["evidence"]["orientation_change_deg"] > 20


def test_accelerometer_offset_is_corrected():
    t, acc, gyro = synthetic([(300, STAND, LIE, 6)])
    # quiet tail rest positions give the orientation spread a sphere fit needs
    rng = np.random.default_rng(3)
    for at in np.linspace(400, 1100, 24):
        m = (t / 1000 >= at) & (t / 1000 < at + 4)
        g = _unit(LIE + rng.normal(0, 0.5, 3))
        acc[m] = g + rng.normal(0, 0.003, (m.sum(), 3))
    biased = acc + np.array([0.18, 0.0, 1.95])
    fixed, info = LD.calibrate_accel(biased, gyro)
    assert info["corrected"] and abs(info["offset"][2] - 1.95) < 0.1
    assert abs(np.median(np.linalg.norm(fixed[gyro.max(1) < 2], axis=1)) - 1) < 0.05
    events = LD.detect(t, biased, gyro, model=None)
    assert len(events) == 1 and events[0]["evidence"]["accel_offset_corrected"]


def test_gap_and_short_input_are_safe():
    t, acc, gyro = synthetic([(300, STAND, LIE, 6)])
    keep = (t < 100_000) | (t > 160_000)
    assert len(LD.detect(t[keep], acc[keep], gyro[keep])) == 1
    with pytest.raises(ValueError):
        LD.prepare(t[::-1], acc, gyro)


def test_posture_labels_follow_alternation():
    events = [dict(code="LYING_DOWN", start_ms=100_000, end_ms=106_000),
              dict(code="STANDING_UP", start_ms=500_000, end_ms=504_000)]
    lab = LD.posture_labels(events, 700)
    assert (lab[:90] == 0).all() and (lab[120:490] == 1).all() and (lab[520:] == 0).all()
    assert (lab[100:106] == -1).all()
    inconsistent = LD.posture_labels([dict(code="LYING_DOWN", start_ms=10_000, end_ms=12_000),
                                      dict(code="LYING_DOWN", start_ms=50_000, end_ms=52_000)], 100)
    assert (inconsistent[20:45] == -1).all()


def _tiny_model():
    from sklearn.ensemble import RandomForestClassifier
    rng = np.random.default_rng(1)
    xe = rng.normal(size=(200, len(LD.MODEL_FEATURES)))
    ye = (xe[:, LD.MODEL_FEATURES.index("dz_l")] < 0).astype(int)
    xp = rng.normal(size=(200, len(LD.POSTURE_FEATURES)))
    yp = (xp[:, 2] < 0).astype(int)
    fe = RandomForestClassifier(8, max_depth=4, random_state=0).fit(xe.astype(np.float32), ye)
    fp = RandomForestClassifier(8, max_depth=4, random_state=0).fit(xp.astype(np.float32), yp)
    model = export_forest(fe, LD.MODEL_FEATURES, np.zeros(len(LD.MODEL_FEATURES)))
    model.update(code="LYING_DOWN", feature_version="event-shape-1", threshold=0.3, algorithm=LD.ALGORITHM,
                 posture=export_forest(fp, LD.POSTURE_FEATURES, np.zeros(len(LD.POSTURE_FEATURES))),
                 params=dict(LD.DEFAULT_PARAMS))
    return model


def test_numeric_model_path_runs_without_pickle():
    model = _tiny_model()
    predict_forest(model, np.zeros((1, len(LD.MODEL_FEATURES))))
    t, acc, gyro = synthetic([(300, STAND, LIE, 6)])
    events = LD.detect(t, acc, gyro, model=json.loads(json.dumps(model)))
    assert all(0 <= e["start_ms"] <= e["point_ms"] <= e["end_ms"] <= t[-1] for e in events)
    assert events == sorted(events, key=lambda e: e["point_ms"])


def _motion_json(path, t, acc, gyro):
    frames = np.zeros(len(t), dtype=np.dtype([("ts", "<u4"), ("axes", "<i2", (9,))]))
    frames["ts"] = (t + 15).astype(np.uint32)
    frames["axes"][:, 0:3] = np.clip(np.round(acc * 4096), -32768, 32767)
    frames["axes"][:, 3:6] = np.clip(np.round(gyro * 32), -32768, 32767)
    doc = dict(device="TEST", uid=1, version=2, create_time=1787936556035, update_time=1787940156035,
               imu=base64.b64encode(frames.tobytes()).decode("ascii"))
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_evidence_dispatch_uses_raw_signal(tmp_path):
    from cowmata_tailring.algorithms.evidence import infer_features
    t, acc, gyro = synthetic([(300, STAND, LIE, 6)])
    raw = tmp_path / "rec_raw.json"
    _motion_json(raw, t, acc, gyro)
    model = _tiny_model()
    (tmp_path / "lying_down.json").write_text(json.dumps(model), encoding="utf-8")
    suite = dict(root=tmp_path, models=[dict(code="LYING_DOWN", file="lying_down.json", threshold=0.3)])
    events = infer_features(suite, dict(source=str(raw), names=["unused"]), ["LYING_DOWN"])
    assert all(e["code"] == "LYING_DOWN" and e["method"] == LD.ALGORITHM for e in events)
    law = LD.detect_motion_file(raw, None)
    assert len(law) == 1 and 296_000 <= law[0]["point_ms"] <= 310_000