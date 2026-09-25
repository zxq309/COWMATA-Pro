"""COWMATA tail-ring STANDING_UP detector v2 — inference (numpy/scipy only).

Pipeline
  1. preprocess      : 25 Hz grid, accelerometer bias correction, gravity direction, dynamics
  2. candidates      : persistent gravity-direction step (±18 s medians) bound to an activity burst
  3. features        : rotation-tolerant physical descriptors of the candidate
  4. scoring         : 3-class (other / STANDING_UP / LYING_DOWN) logistic model + optional 1D-CNN
  5. decoding        : posture alternation (lying -> standing only via STANDING_UP) — Viterbi
  6. boundaries      : start = first dynamics >= 25 % of burst peak; end = first 2 s settle after
                       half of the orientation change is done
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.special import erf

from .features import FEATURE_NAMES, candidate_features, record_reference
from .signal import DEFAULT_PARAMS, candidates, preprocess

MODEL_SCHEMA = "cowmata-standup-2"
ABSOLUTE = {"d_x", "d_y", "pre_x", "pre_y", "pre_z", "post_x", "post_y", "post_z"}
INVARIANT = [n for n in FEATURE_NAMES if n not in ABSOLUTE]
INV_IDX = [FEATURE_NAMES.index(n) for n in INVARIANT]

# CNN window: 25 Hz signal decimated by 2 -> 12.5 Hz; 300 samples (24 s) before onset, 450 after.
WIN_STEP, WIN_PRE, WIN_LEN = 2, 300, 750


# ----------------------------------------------------------------------------- windows
def candidate_windows(sig, cands):
    step = WIN_STEP
    d, dyn, gm, t = sig["dir"][::step], sig["dyn"][::step], sig["gmag"][::step], sig["t"][::step]
    out = np.zeros((len(cands), 6, WIN_LEN), np.float32)
    for k, c in enumerate(cands):
        i = int(np.searchsorted(t, c["t0"]))
        a, b = i - WIN_PRE, i - WIN_PRE + WIN_LEN
        lo, hi = max(a, 0), min(b, len(t))
        if hi <= lo:
            continue
        seg = slice(lo - a, hi - a)
        out[k, 0:3, seg] = d[lo:hi].T
        out[k, 3, seg] = np.log(dyn[lo:hi] + 1e-3)
        out[k, 4, seg] = np.log(gm[lo:hi] + 0.1)
        out[k, 5, seg] = 1.0
    return out


def cnn_input(w):
    """Absolute direction + direction relative to the pre-onset median + log dynamics + mask."""
    d = w[:, 0:3]
    ref = np.median(d[:, :, WIN_PRE - 200:WIN_PRE - 10], axis=-1, keepdims=True)
    return np.concatenate([d, d - ref, w[:, 3:6]], axis=1).astype(np.float32)


# ----------------------------------------------------------------------------- numpy CNN
def _conv1d(x, w, b):
    k = w.shape[-1]
    p = k // 2
    xp = np.pad(x, ((0, 0), (0, 0), (p, p)))
    win = np.lib.stride_tricks.sliding_window_view(xp, k, axis=2)  # N,C,L,k
    return np.einsum("nclk,ock->nol", win, w, optimize=True) + b[None, :, None]


def cnn_forward(params, x):
    h = x
    for layer in params["layers"]:
        h = _conv1d(h, layer["w"], layer["b"])
        h = h * layer["bn_scale"][None, :, None] + layer["bn_shift"][None, :, None]
        h = 0.5 * h * (1 + erf(h / np.sqrt(2)))
        L = h.shape[-1] // 2 * 2
        h = h[..., :L].reshape(h.shape[0], h.shape[1], -1, 2).max(-1)
    h = h.mean(-1)
    z = h @ params["head_w"].T + params["head_b"]
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def load_cnn(path):
    z = np.load(path)
    n = int(z["n_layers"])
    return dict(layers=[dict(w=z[f"w{i}"], b=z[f"b{i}"], bn_scale=z[f"s{i}"], bn_shift=z[f"h{i}"])
                        for i in range(n)], head_w=z["head_w"], head_b=z["head_b"])


def load_cnn_ensemble(path):
    z = np.load(path)
    nets = []
    for s in range(int(z["n_seeds"])):
        g = lambda k: z[f"seed{s}_{k}"]  # noqa: E731
        n = int(g("n_layers"))
        nets.append(dict(layers=[dict(w=g(f"w{i}"), b=g(f"b{i}"), bn_scale=g(f"s{i}"), bn_shift=g(f"h{i}"))
                                 for i in range(n)], head_w=g("head_w"), head_b=g("head_b")))
    return nets


# ----------------------------------------------------------------------------- linear model
def lr_predict(m, X):
    x = np.where(np.isfinite(X), X, np.asarray(m["median"]))
    x = (x - np.asarray(m["mean"])) / np.asarray(m["scale"])
    z = x @ np.asarray(m["coef"]).T + np.asarray(m["intercept"])
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


# ----------------------------------------------------------------------------- decoding
def viterbi_standups(P, eps=0.02, prior_standing=0.5):
    """P: (n,3) other/SU/LD in time order. Returns bool mask of accepted STANDING_UP."""
    n = len(P)
    if n == 0:
        return np.zeros(0, bool)
    lg = lambda v: np.log(np.maximum(v, 1e-12))  # noqa: E731
    score = np.array([lg(1 - prior_standing), lg(prior_standing)])
    back = []
    for po, ps, pl in P:
        to0 = np.array([score[0] + lg(po + eps * pl), score[1] + lg(pl)])
        to1 = np.array([score[0] + lg(ps), score[1] + lg(po + eps * ps)])
        back.append((int(np.argmax(to0)), int(np.argmax(to1))))
        score = np.array([to0.max(), to1.max()])
    s = int(np.argmax(score))
    states = [s]
    for b in reversed(back):
        s = b[s]
        states.append(s)
    states = states[::-1]
    return np.array([states[k] == 0 and states[k + 1] == 1 for k in range(n)])


# ----------------------------------------------------------------------------- boundaries
def refine_boundaries(sig, c, onset_ratio=0.25, settle_ratio=0.25, settle_hold_s=2.0, progress=0.5,
                      max_len_s=60.0):
    fs = sig["fs"]
    d, dyn, t = sig["dir"], sig["dyn"], sig["t"]
    n = len(t)
    a, b = c["i0"], c["i1"]
    pre = np.median(d[max(0, a - int(20 * fs)):max(1, a - int(fs))], 0)
    post = np.median(d[min(n - 1, b + int(fs)):min(n, b + int(20 * fs))], 0)
    v = post - pre
    lo, hi = max(0, a - int(10 * fs)), min(n, b + int(10 * fs))
    seg = dyn[lo:hi]
    pk = float(seg.max())
    on = lo + int(np.flatnonzero(seg >= onset_ratio * pk)[0])
    prog = uniform_filter1d(((d - pre) @ v) / max(float(v @ v), 1e-6), int(fs))
    h = int(settle_hold_s * fs)
    busy = uniform_filter1d((dyn >= settle_ratio * pk).astype(float), h, origin=-(h // 2)) > 0
    stop = min(n, on + int(max_len_s * fs))
    idx = np.flatnonzero(~busy[on:stop] & (prog[on:stop] >= progress))
    end = on + int(idx[0]) if len(idx) else b - 1
    return float(t[on]), float(t[max(end, on + 1)])


# ----------------------------------------------------------------------------- detector
class StandupDetector:
    def __init__(self, model_dir):
        model_dir = Path(model_dir)
        self.meta = json.loads((model_dir / "model.json").read_text(encoding="utf-8"))
        if self.meta.get("schema") != MODEL_SCHEMA:
            raise ValueError("Incompatible stand-up model")
        self.params = {**DEFAULT_PARAMS, **self.meta.get("candidate_params", {})}
        cnn = self.meta.get("cnn_file")
        self.cnn = load_cnn_ensemble(model_dir / cnn) if cnn else None

    def score(self, sig, cands):
        ref = record_reference(sig)
        feats = [candidate_features(sig, c, ref) for c in cands]
        keep = [i for i, f in enumerate(feats) if f is not None]
        cands = [cands[i] for i in keep]
        if not cands:
            return cands, np.zeros((0, 3))
        X = np.array([feats[i] for i in keep])[:, INV_IDX]
        P = lr_predict(self.meta["lr"], X)
        if self.cnn is not None:
            w = self.meta.get("cnn_weight", 0.5)
            pc = np.mean([cnn_forward(n, cnn_input(candidate_windows(sig, cands))) for n in self.cnn], 0)
            P = (1 - w) * P + w * pc
        return cands, P

    def detect(self, times_ms, acc_g, gyro_dps):
        sig = preprocess(times_ms, acc_g, gyro_dps)
        cands, _, _ = candidates(sig, self.params)
        cands, P = self.score(sig, cands)
        thr = float(self.meta["threshold"])
        if self.meta.get("decoding") == "viterbi":
            order = np.argsort([c["t0"] for c in cands])
            accept = np.zeros(len(cands), bool)
            accept[order] = viterbi_standups(P[order]) & (P[order, 1] >= self.meta.get("viterbi_floor", thr))
        else:
            accept = P[:, 1] >= thr
        events = []
        for c, p, ok in zip(cands, P, accept):
            if not ok:
                continue
            t0, t1 = refine_boundaries(sig, c)
            events.append(dict(code="STANDING_UP", label="起立过程", start_ms=t0, end_ms=t1,
                               duration_s=round((t1 - t0) / 1000, 3), score=float(p[1]),
                               p_lying_down=float(p[2]), orientation_step=c["step"],
                               sensor_quality=sig["quality"], time_semantics="proposed_interval",
                               review_status="pending", model=self.meta.get("version")))
        return events


def detect_file(model_dir, raw_json_path):
    """Convenience: run on a COWMATA Motion raw JSON using the project's own decoder."""
    from cowmata_tailring.annotation.data import parse_motion_object

    with open(raw_json_path, encoding="utf-8") as fh:
        s = parse_motion_object(json.load(fh), source_path=raw_json_path)
    c = s.channels
    acc = np.column_stack([c["ax"], c["ay"], c["az"]]) / 9.80665
    gyr = np.column_stack([c["gx"], c["gy"], c["gz"]])
    return StandupDetector(model_dir).detect(s.times_ms, acc, gyr)
