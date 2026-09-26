"""Decision model zoo with numeric JSON persistence (no pickle at inference).

Every algorithm is trained through :func:`fit_model` and returns a JSON document; every
prediction, including cross-validation scores, goes through :func:`predict_model`, so the
numbers reported in training are produced by exactly the code that runs in deployment.
"""
from __future__ import annotations

import base64
import math
import warnings

import numpy as np

ALGORITHMS = {
    "expert_rules": dict(title="专家规则打分（文献先验 + Platt 校准）", family="知识驱动",
                         note="按文献方向对各特征相对本牛基线的 z 分数加权；只学习 2 个校准参数，小样本最稳"),
    "baseline_deviation": dict(title="本牛基线偏离度（多变量异常检测）", family="统计过程控制",
                               note="各主特征 72 h 稳健 z 分数的均方根，类似 Hotelling T²；无需知道方向"),
    "logistic": dict(title="L2 逻辑回归", family="广义线性模型", note="可解释系数；缺失值中位数填补 + 缺失指示"),
    "decision_tree": dict(title="决策树 CART", family="树模型", note="深度 ≤5，规则可读"),
    "random_forest": dict(title="随机森林", family="Bagging 集成", note="300 棵树，按牛分组验证"),
    "extra_trees": dict(title="极端随机树", family="Bagging 集成", note="随机切分，方差更低"),
    "adaboost": dict(title="AdaBoost（SAMME，深度 2 树）", family="Boosting 集成", note="经典自适应提升"),
    "xgboost": dict(title="XGBoost 梯度提升", family="Boosting 集成", note="原生缺失值处理，记录逐轮训练曲线"),
    "stacking": dict(title="堆叠集成（LR+RF+XGB→LR）+ 等渗校准", family="元学习集成",
                     note="内层按牛交叉拟合生成元特征，避免泄漏"),
}
DEFAULT_ALGORITHMS = ("expert_rules", "baseline_deviation", "logistic", "decision_tree", "random_forest",
                      "extra_trees", "adaboost", "xgboost", "stacking")
MODEL_SCHEMA = "cowmata-decision-model-4.3.4"

# Literature priors: (direction, weight, derivation, scale). direction +1 = rises before calving,
# -1 = falls, 0 = |deviation|. signal = direction * value / scale, clipped to [0, 8].
PRIORS = {
    "temperature": (-1, 1.0, "z72", 1.0),        # 产前 12–24 h 体温下降 0.3–0.5 °C
    "straining_ratio": (+1, 1.5, "1h", 0.02),    # 努责只在娩出前数小时出现；远离产犊 98% 窗为 0，用 1 h 占比
    "activity": (+1, 0.6, "z72", 1.0),           # 产前 6–12 h 不安、走动增加
    "lying_ratio": (0, 0.6, "z72", 1.0),         # 躺卧回合增多、躺卧时长变化方向因牛而异
    "gyro_spectral_entropy": (0, 0.5, "z72", 1.0),  # 尾部运动复杂度（抬尾、摆尾）变化
    "heart_rate": (+1, 0.4, "z72", 1.0),         # 分娩期心率升高
    "spo2": (0, 0.2, "z72", 1.0),                # 证据弱，只作辅助
}


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


# ----------------------------------------------------------------------------- preprocessing

def prep_fit(x):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        median = np.nanmedian(x, axis=0)
    median = np.where(np.isfinite(median), median, 0.0)
    filled = np.where(np.isfinite(x), x, median)
    mean, std = filled.mean(axis=0), filled.std(axis=0)
    std = np.where(std > 1e-9, std, 1.0)
    return dict(median=median.tolist(), mean=mean.tolist(), std=std.tolist())


def prep_apply(prep, x, *, scale=False, indicators=True):
    median = np.asarray(prep["median"])
    missing = ~np.isfinite(x)
    filled = np.where(missing, median, x)
    if scale:
        filled = (filled - np.asarray(prep["mean"])) / np.asarray(prep["std"])
    return np.column_stack([filled, missing.astype(float)]) if indicators else filled


def balanced_weights(y):
    y = np.asarray(y)
    pos = max(1, int(y.sum()))
    neg = max(1, len(y) - pos)
    return np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))


# ----------------------------------------------------------------------------- trees → JSON

def _tree_doc(tree, *, kind="proba"):
    t = tree.tree_
    value = t.value[:, 0]
    if kind == "proba":
        leaf = (value[:, 1] / np.maximum(value.sum(axis=1), 1e-12)).tolist() if value.shape[1] > 1 else value[:, 0].tolist()
    else:
        leaf = value[:, 0].tolist()
    return dict(left=t.children_left.tolist(), right=t.children_right.tolist(), feature=t.feature.tolist(),
                threshold=[float(v) for v in t.threshold], value=leaf)


def _tree_predict(doc, z):
    left, right = np.asarray(doc["left"]), np.asarray(doc["right"])
    feature, threshold = np.asarray(doc["feature"]), np.asarray(doc["threshold"])
    value = np.asarray(doc["value"], dtype=float)
    n = len(left)
    inner = left >= 0
    if (n == 0 or any(len(a) != n for a in (right, feature, threshold, value))
            or np.any(left[inner] <= np.flatnonzero(inner)) or np.any(right[inner] <= np.flatnonzero(inner))
            or np.any(left[inner] >= n) or np.any(right[inner] >= n)
            or np.any(feature[inner] >= z.shape[1]) or np.any(feature[inner] < 0)):
        raise ValueError("决策树结构无效")
    node = np.zeros(len(z), dtype=int)
    while True:
        active = np.flatnonzero(left[node] >= 0)
        if not len(active):
            break
        current = node[active]
        node[active] = np.where(z[active, feature[current]] <= threshold[current], left[current], right[current])
    return value[node]


# ----------------------------------------------------------------------------- expert scores

def _prior_columns(columns, primaries):
    """[(index, direction, weight, key, scale)] for each feature key's primary trend column."""
    result = []
    for key, (direction, weight, deriv, scale) in PRIORS.items():
        primary = primaries.get(key)
        candidates = []
        for d in (deriv, "z72", "1h"):
            if primary:
                candidates.append(f"{primary}@{d}")
                candidates.append(f"{key}.{primary}@{d}")
        for name in candidates:
            if name in columns:
                result.append((columns.index(name), direction, weight, key, scale if name.endswith("@" + deriv) else 1.0))
                break
    return result


def _expert_raw(doc, x):
    total = np.zeros(len(x))
    weight_sum = np.zeros(len(x))
    for index, direction, weight, _, scale in doc["priors"]:
        z = x[:, index] / scale
        ok = np.isfinite(z)
        signal = np.abs(z) if direction == 0 else direction * z
        total += np.where(ok, weight * np.clip(signal, 0, 8), 0.0)
        weight_sum += np.where(ok, weight, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(weight_sum > 0, total / np.maximum(weight_sum, 1e-9), 0.0)


def _deviation_raw(doc, x):
    idx = [i for i, _, _, _, scale in doc["priors"]]
    scales = np.asarray([scale for *_, scale in doc["priors"]], dtype=float)
    if not idx:
        return np.zeros(len(x))
    z = x[:, idx] / scales
    ok = np.isfinite(z)
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        value = np.sqrt(np.nanmean(np.where(ok, np.clip(z, -10, 10) ** 2, np.nan), axis=1))
    return np.where(np.isfinite(value), value, 0.0)


def _platt(score, y, w=None):
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(C=1e3, max_iter=2000)
    model.fit(score.reshape(-1, 1), y, sample_weight=w)
    return float(model.coef_[0, 0]), float(model.intercept_[0])


# ----------------------------------------------------------------------------- fit / predict

def fit_model(algorithm, x, y, columns, *, groups=None, primaries=None, seed=433, curves=None, eval_set=None):
    """Train one algorithm and return its JSON document."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=int)
    if set(np.unique(y)) != {0, 1}:
        raise ValueError("训练数据需同时包含正例与负例")
    doc = dict(schema=MODEL_SCHEMA, algorithm=algorithm, columns=list(columns))
    w = balanced_weights(y)
    if algorithm in ("expert_rules", "baseline_deviation"):
        doc["priors"] = _prior_columns(list(columns), primaries or {})
        raw = (_expert_raw if algorithm == "expert_rules" else _deviation_raw)(doc, x)
        doc["platt"] = _platt(raw, y, w) if doc["priors"] else [0.0, float(_logit(np.array([y.mean()]))[0])]
        return doc
    prep = prep_fit(x)
    doc["prep"] = prep
    if algorithm == "logistic":
        from sklearn.linear_model import LogisticRegression

        z = prep_apply(prep, x, scale=True)
        model = LogisticRegression(C=0.1, max_iter=5000)
        model.fit(z, y, sample_weight=w)
        doc.update(coef=model.coef_[0].tolist(), intercept=float(model.intercept_[0]))
        return doc
    if algorithm in ("decision_tree", "random_forest", "extra_trees"):
        from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
        from sklearn.tree import DecisionTreeClassifier

        z = prep_apply(prep, x)
        if algorithm == "decision_tree":
            model = DecisionTreeClassifier(max_depth=5, min_samples_leaf=40, random_state=seed)
            model.fit(z, y, sample_weight=w)
            trees = [model]
        else:
            cls = RandomForestClassifier if algorithm == "random_forest" else ExtraTreesClassifier
            model = cls(n_estimators=300, max_depth=8, min_samples_leaf=20, max_features="sqrt",
                        n_jobs=2, random_state=seed)
            model.fit(z, y, sample_weight=w)
            trees = model.estimators_
        doc["trees"] = [_tree_doc(t) for t in trees]
        doc["importance"] = model.feature_importances_[: len(columns)].tolist()
        return doc
    if algorithm == "adaboost":
        from sklearn.ensemble import AdaBoostClassifier
        from sklearn.tree import DecisionTreeClassifier

        z = prep_apply(prep, x)
        model = AdaBoostClassifier(DecisionTreeClassifier(max_depth=2, min_samples_leaf=20),
                                   n_estimators=150, learning_rate=0.3, random_state=seed)
        model.fit(z, y, sample_weight=w)
        weights = model.estimator_weights_[: len(model.estimators_)]
        doc["trees"] = [_tree_doc(t) for t in model.estimators_]
        doc["weights"] = [float(v) for v in weights]
        doc["importance"] = model.feature_importances_[: len(columns)].tolist()
        return doc
    if algorithm == "xgboost":
        import xgboost as xgb

        params = dict(objective="binary:logistic", eval_metric=["logloss", "auc"], max_depth=4, eta=0.05,
                      subsample=0.8, colsample_bytree=0.6, min_child_weight=5, reg_lambda=2.0,
                      tree_method="hist", nthread=2, seed=seed)
        train = xgb.DMatrix(x, label=y, weight=w, missing=np.nan)
        evals = [(train, "train")]
        if eval_set is not None:
            evals.append((xgb.DMatrix(np.asarray(eval_set[0], dtype=float), label=eval_set[1], missing=np.nan), "valid"))
        history = {}
        booster = xgb.train(params, train, num_boost_round=300, evals=evals, evals_result=history, verbose_eval=False)
        if curves is not None:
            curves.update(history)
        doc["booster"] = base64.b64encode(bytes(booster.save_raw("json"))).decode("ascii")
        scores = booster.get_score(importance_type="gain")
        doc["importance"] = [float(scores.get(f"f{i}", 0.0)) for i in range(len(columns))]
        return doc
    if algorithm == "stacking":
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import GroupKFold

        bases = ("logistic", "random_forest", "xgboost")
        groups = np.asarray(groups if groups is not None else np.arange(len(y)))
        meta = np.full((len(y), len(bases)), np.nan)
        splits = min(3, len(set(groups)))
        for train, test in GroupKFold(n_splits=splits).split(x, y, groups):
            if len(set(y[train])) < 2:
                continue
            for j, base in enumerate(bases):
                inner = fit_model(base, x[train], y[train], columns, seed=seed + j)
                meta[test, j] = predict_model(inner, x[test])
        ok = np.isfinite(meta).all(axis=1)
        if ok.sum() < 20 or len(set(y[ok])) < 2:
            raise ValueError("堆叠集成的内层交叉拟合样本不足")
        stacker = LogisticRegression(C=1.0, max_iter=2000)
        stacker.fit(_logit(meta[ok]), y[ok])
        combined = _sigmoid(_logit(meta[ok]) @ stacker.coef_[0] + stacker.intercept_[0])
        from sklearn.isotonic import IsotonicRegression

        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(combined, y[ok])
        doc["bases"] = [fit_model(base, x, y, columns, seed=seed + j) for j, base in enumerate(bases)]
        doc["meta"] = dict(coef=stacker.coef_[0].tolist(), intercept=float(stacker.intercept_[0]))
        doc["isotonic"] = dict(x=iso.X_thresholds_.tolist(), y=iso.y_thresholds_.tolist())
        return doc
    raise ValueError(f"未知决策算法：{algorithm}")


def predict_model(doc, x):
    """Probability-like score in [0, 1] for each row of ``x`` (columns in ``doc['columns']`` order)."""
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(doc["columns"]):
        raise ValueError("决策模型输入列数不一致")
    algorithm = doc["algorithm"]
    if algorithm in ("expert_rules", "baseline_deviation"):
        raw = (_expert_raw if algorithm == "expert_rules" else _deviation_raw)(doc, x)
        a, b = doc["platt"]
        return _sigmoid(a * raw + b)
    if algorithm == "logistic":
        z = prep_apply(doc["prep"], x, scale=True)
        return _sigmoid(z @ np.asarray(doc["coef"]) + doc["intercept"])
    if algorithm in ("decision_tree", "random_forest", "extra_trees"):
        z = prep_apply(doc["prep"], x)
        return np.mean([_tree_predict(t, z) for t in doc["trees"]], axis=0)
    if algorithm == "adaboost":
        z = prep_apply(doc["prep"], x)
        votes = np.zeros(len(z))
        for tree, weight in zip(doc["trees"], doc["weights"]):
            votes += weight * np.where(_tree_predict(tree, z) >= 0.5, 1.0, -1.0)
        return _sigmoid(2.0 * votes / max(sum(doc["weights"]), 1e-9) * 2.0)
    if algorithm == "xgboost":
        import xgboost as xgb

        booster = xgb.Booster(params=dict(nthread=2))
        booster.load_model(bytearray(base64.b64decode(doc["booster"])))
        return booster.predict(xgb.DMatrix(x, missing=np.nan))
    if algorithm == "stacking":
        meta = np.column_stack([predict_model(base, x) for base in doc["bases"]])
        combined = _sigmoid(_logit(meta) @ np.asarray(doc["meta"]["coef"]) + doc["meta"]["intercept"])
        return np.interp(combined, doc["isotonic"]["x"], doc["isotonic"]["y"])
    raise ValueError(f"未知决策算法：{algorithm}")


def importance(doc):
    """Global importance per input column (normalised to sum 1), when the model defines one."""
    columns = doc["columns"]
    if "importance" in doc:
        values = np.asarray(doc["importance"], dtype=float)
    elif doc["algorithm"] == "logistic":
        values = np.abs(np.asarray(doc["coef"][: len(columns)]))
    elif doc["algorithm"] in ("expert_rules", "baseline_deviation"):
        values = np.zeros(len(columns))
        for index, _, weight, _, _ in doc.get("priors", []):
            values[index] = weight
    elif doc["algorithm"] == "stacking":
        values = np.mean([np.asarray(list(importance(b).values())) for b in doc["bases"]], axis=0)
    else:
        values = np.zeros(len(columns))
    total = float(np.sum(np.abs(values)))
    return {c: float(abs(v) / total) if total > 0 else 0.0 for c, v in zip(columns, values)}


# ----------------------------------------------------------------------------- time to calving

QUANTILES = (0.1, 0.5, 0.9)


def fit_time_to_event(x, hours, columns, *, seed=433, max_hours=240.0):
    """Quantile gradient boosting of log1p(hours to calving) → P10 / P50 / P90 hours."""
    import xgboost as xgb

    x = np.asarray(x, dtype=float)
    hours = np.asarray(hours, dtype=float)
    ok = np.isfinite(hours) & (hours > 0) & (hours <= max_hours)
    if ok.sum() < 50:
        raise ValueError("剩余时间回归样本不足")
    params = dict(objective="reg:quantileerror", quantile_alpha=np.asarray(QUANTILES), max_depth=4, eta=0.05,
                  subsample=0.8, colsample_bytree=0.6, min_child_weight=5, tree_method="hist", nthread=2, seed=seed)
    booster = xgb.train(params, xgb.DMatrix(x[ok], label=np.log1p(hours[ok]), missing=np.nan), num_boost_round=300)
    return dict(schema=MODEL_SCHEMA, algorithm="time_to_event_quantile", columns=list(columns), quantiles=list(QUANTILES),
                max_hours=max_hours, booster=base64.b64encode(bytes(booster.save_raw("json"))).decode("ascii"))


def predict_time_to_event(doc, x):
    import xgboost as xgb

    booster = xgb.Booster(params=dict(nthread=2))
    booster.load_model(bytearray(base64.b64decode(doc["booster"])))
    raw = np.asarray(booster.predict(xgb.DMatrix(np.asarray(x, dtype=float), missing=np.nan)))
    raw = np.sort(raw.reshape(len(x), -1), axis=1)
    q = float(doc.get("conformal_log", 0.0))  # CQR widening of P10/P90 in log space
    raw[:, 0] -= q
    raw[:, -1] += q
    hours = np.clip(np.expm1(raw), 0.0, doc.get("max_hours", 240.0))
    return hours  # columns follow doc["quantiles"]


def conformal_offset(pred_hours, hours, coverage=0.8):
    """Conformalised quantile regression offset (log1p space) from held-out P10/P90."""
    lo, hi, y = np.log1p(pred_hours[:, 0]), np.log1p(pred_hours[:, -1]), np.log1p(hours)
    ok = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(y)
    if ok.sum() < 20:
        return 0.0
    score = np.maximum(lo[ok] - y[ok], y[ok] - hi[ok])
    return float(max(0.0, np.quantile(score, min(1.0, coverage * (1 + 1 / ok.sum())))))


def finite(value):
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)
