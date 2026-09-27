"""Numeric forest: sklearn ExtraTrees exported to COWMATA ``numeric-forest-1`` JSON.

The JSON matches ``cowmata_tailring.algorithms.models.export_forest`` so the
app can score it with ``predict_forest`` - no pickle is ever loaded.
"""
from __future__ import annotations

import numpy as np

MAX_TREES = 256
MAX_NODES = 20000


def fit_forest(x, y, w, *, n_estimators=256, min_samples_leaf=2, seed=20260925):
    from sklearn.ensemble import ExtraTreesClassifier

    model = ExtraTreesClassifier(n_estimators=min(n_estimators, MAX_TREES), min_samples_leaf=min_samples_leaf,
                                 max_features="sqrt", bootstrap=False, n_jobs=-1, random_state=seed)
    median = np.nanmedian(np.where(np.isfinite(x), x, np.nan), axis=0)
    median = np.where(np.isfinite(median), median, 0.0)
    xx = np.where(np.isfinite(x), x, median)
    model.fit(xx, y, sample_weight=w)
    return model, median


def export_forest(model, feature_names, median) -> dict:
    if list(model.classes_) != [0, 1]:
        raise ValueError("需要同时有正样本和背景样本")
    trees = []
    for est in model.estimators_:
        t = est.tree_
        if t.node_count > MAX_NODES:
            raise ValueError("树过大，无法导出")
        v = t.value[:, 0]
        prob = v[:, 1] / np.maximum(v.sum(axis=1), 1e-12)
        trees.append(dict(left=t.children_left.tolist(), right=t.children_right.tolist(), feature=t.feature.tolist(),
                          threshold=t.threshold.tolist(), probability=prob.tolist()))
    return dict(schema="numeric-forest-1", features=list(feature_names), median=np.asarray(median).tolist(), trees=trees)


def predict_forest(model: dict, x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32).copy()
    if model.get("schema") != "numeric-forest-1" or x.ndim != 2 or x.shape[1] != len(model["features"]):
        raise ValueError("模型特征结构不兼容")
    median = np.asarray(model["median"], dtype=np.float32)
    bad = ~np.isfinite(x)
    x[bad] = np.broadcast_to(median, x.shape)[bad]
    trees = model["trees"]
    if not 0 < len(trees) <= MAX_TREES:
        raise ValueError("森林规模无效")
    out = np.zeros(len(x))
    rows = np.arange(len(x))
    for tree in trees:
        left, right = np.asarray(tree["left"]), np.asarray(tree["right"])
        feat, thr = np.asarray(tree["feature"]), np.asarray(tree["threshold"])
        prob = np.asarray(tree["probability"])
        node = np.zeros(len(x), dtype=np.int64)
        for _ in range(len(left)):
            inner = left[node] >= 0
            if not inner.any():
                break
            r, nd = rows[inner], node[inner]
            node[inner] = np.where(x[r, feat[nd]] <= thr[nd], left[nd], right[nd])
        out += prob[node]
    return out / len(trees)
