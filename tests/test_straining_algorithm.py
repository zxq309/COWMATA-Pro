import json

import numpy as np


def _record(seed, strain=(300, 345), wag=(150, 175), seconds=480):
    """Synthetic 50 Hz lying-cow tail record: quiet tilt pulses every 3.5 s vs. gyro-rich wagging."""
    rng = np.random.default_rng(seed)
    t = np.arange(seconds * 50) * 20.0
    s = t / 1000
    tilt = np.zeros_like(s)
    for c in np.arange(strain[0] + 1, strain[1] - 1, 3.5):
        tilt += np.radians(5) * np.exp(-0.5 * ((s - c) / 0.35) ** 2)
    wag_on = (s >= wag[0]) & (s < wag[1])
    wag_angle = np.where(wag_on, np.radians(25) * np.sin(2 * np.pi * 2.2 * s), 0)
    base = np.radians(35)
    ax = np.sin(base + tilt) * np.cos(wag_angle)
    ay = -np.cos(base + tilt)
    az = np.sin(base + tilt) * np.sin(wag_angle)
    acc = np.column_stack([ax, ay, az]) + rng.normal(0, 0.004, (len(t), 3))
    gyr = np.column_stack(
        [np.gradient(np.degrees(tilt), s), np.zeros_like(s), np.gradient(np.degrees(wag_angle), s)]
    )
    gyr += rng.normal(0, 0.8, gyr.shape)
    return t, acc, gyr


def test_quiet_pulse_train_separates_straining_from_tail_wagging():
    from cowmata_tailring.algorithms.straining import signal

    t, acc, gyr = _record(1)
    f = signal.second_features(t, acc, gyr)
    q = f["X"][:, f["names"].index("quiet_pulses_24s")]
    hf = f["X"][:, f["names"].index("log_hf_mean_5s")]
    assert np.nanmedian(q[305:340]) >= 5
    assert np.nanmax(q[140:185]) <= 2
    assert np.nanmedian(hf[155:170]) > np.nanmedian(hf[305:340]) + 1
    pulses = f["pulses"]
    inside = (pulses["t"] > 300) & (pulses["t"] < 345)
    assert pulses["quiet"][inside].mean() > 0.9
    ipi = np.diff(np.sort(pulses["t"][inside & pulses["quiet"]]))
    assert abs(np.median(ipi) - 3.5) < 0.3


def test_segmentation_requires_contraction_pulses_and_breaks_at_gaps():
    from cowmata_tailring.algorithms.straining import signal

    scores = np.zeros(200)
    scores[50:80] = 0.9
    scores[120:140] = 0.9
    valid = np.ones(200, bool)
    pulses = dict(t=np.arange(52, 78, 3.5), quiet=np.ones(8, bool))
    events, _ = signal.segment(scores, valid, pulses, dict(high=0.6, low=0.4))
    assert (
        len(events) == 1
        and 48000 <= events[0]["start_ms"] <= 52000
        and events[0]["contraction_pulses"] >= 7
    )


def test_bundle_round_trip_detects_only_the_contraction_train(tmp_path):
    from cowmata_tailring.algorithms.models import export_forest
    from cowmata_tailring.algorithms.straining import detect, load_bundle, signal, train
    from cowmata_tailring.algorithms.straining.candidates import candidate_features, candidate_names

    feats, labels = [], []
    for seed in range(4):
        f = signal.second_features(*_record(seed))
        y = np.zeros(len(f["X"]), int)
        y[300:345] = 1
        feats.append(f)
        labels.append(y)
    X = np.vstack([f["X"][f["valid"]] for f in feats])
    Y = np.concatenate([y[f["valid"]] for f, y in zip(feats, labels)])
    m1, med1 = train._forest(X, Y, dict(n_estimators=16, max_depth=6, min_samples_leaf=3), 1)
    seg = dict(signal.DEFAULT_SEGMENTATION, high=0.45, low=0.3)
    cx, cy = [], []
    for f in feats:
        sc = train._predict(m1, med1, f["X"])
        ev, sm = signal.segment(sc, f["valid"], f["pulses"], seg, f["duration_ms"])
        cx.append(candidate_features(f, ev, sm))
        cy += [int(e["start_ms"] < 345000 and e["end_ms"] > 300000) for e in ev]
    cx.append(np.zeros((1, len(candidate_names())), np.float32))
    cy.append(0)
    m2, med2 = train._forest(
        np.vstack(cx), np.array(cy), dict(n_estimators=16, max_depth=4, min_samples_leaf=1), 1
    )
    bundle = dict(
        schema="cowmata-straining-bundle-1",
        code="STRAINING_BOUT",
        version="test",
        feature_version=signal.FEATURE_VERSION,
        stage1_feature_names=feats[0]["names"],
        stage1=export_forest(m1, feats[0]["names"], med1),
        stage2=export_forest(m2, candidate_names(), med2),
        segmentation=seg,
        stage2_threshold=0.5,
    )
    (tmp_path / "straining_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    loaded = load_bundle(tmp_path)
    events = detect(loaded, *_record(99))
    assert events, "the synthetic contraction train must be detected"
    assert all(e["start_ms"] < 350000 and e["end_ms"] > 295000 for e in events)
    assert all(e["requires_review"] for e in events)
