"""Stable JSON API of the COWMATA engine (``cowmata-engine-1``).

One entry point, :func:`handle`, takes a JSON-compatible request ``{"action": ..., ...}`` and
returns ``{"api", "ok", "action", "result" | "error"}``. The desktop worker, the command line
(``python -m cowmata_engine``) and the local HTTP service (``cowmata_engine.server``) all route
through it, so an external front-end sees exactly what the desktop sees. See
``docs/ENGINE_API_433.md`` for request / response fields.
"""
from __future__ import annotations

import traceback

from . import ENGINE_API, __version__


def _engine_info(_request, **_):
    from .decision.models import ALGORITHMS
    from .features import FEATURE_MODULES

    return dict(api=ENGINE_API, version=__version__, actions=sorted(ACTIONS),
                features=list(FEATURE_MODULES), algorithms=list(ALGORITHMS))


def _features_catalog(_request, **_):
    from .features import feature_catalog

    return feature_catalog()


def _features_extract(request, progress, cancelled):
    from .decision.dataset import extract_feature_tables

    tables, issues, catalog = extract_feature_tables(
        request["root"], keys=request.get("keys"), workers=request.get("workers"), output=request["output"],
        progress=progress, cancelled=cancelled)
    return dict(output=request["output"], rows={k: len(v) for k, v in tables.items()}, issues=issues[:500],
                issue_count=len(issues), catalog=catalog)


def _build_dataset(request, progress, cancelled):
    from .decision.dataset import build_dataset

    return build_dataset(output=request["output"], features_root=request.get("features_root"),
                         raw_root=request.get("raw_root"), ledger=request.get("ledger"),
                         calving_dataset=request.get("calving_dataset"), keys=request.get("keys"),
                         workers=request.get("workers"), lookback_days=request.get("lookback_days", 10),
                         progress=progress, cancelled=cancelled)


def _train(request, progress, cancelled):
    from .decision.models import DEFAULT_ALGORITHMS
    from .decision.train import train_decision

    return train_decision(request["dataset"], request["output"],
                          algorithms=tuple(request.get("algorithms") or DEFAULT_ALGORITHMS),
                          horizon=int(request.get("horizon", 24)), folds=int(request.get("folds", 5)),
                          persistence=int(request.get("persistence", 2)), features=request.get("features"),
                          require=request.get("require"), progress=progress, cancelled=cancelled)


def _predict_folder(request, progress, cancelled):
    from .decision.predict import predict_folder

    return predict_folder(request["folder"], request["model"], request["output"], workers=request.get("workers"),
                          features_root=request.get("features_root"), progress=progress, cancelled=cancelled)


def _predict_windows(request, progress, cancelled):
    """Front-end supplies feature windows directly: {"windows": {key: [row, ...]}, "model": path}."""
    from .decision.dataset import build_decision_rows
    from .decision.predict import coverage_summary, predict_rows
    from .features import FEATURE_MODULES, load_feature

    tables, manifests = {}, {}
    for key, rows in (request.get("windows") or {}).items():
        if key not in FEATURE_MODULES:
            raise ValueError(f"未知特征：{key}")
        present = {c for r in rows for c in r} - {"cow_id", "device_id", "field_mark", "start_epoch_ms", "end_epoch_ms",
                                                   "available_epoch_ms", "coverage", "source"}
        try:
            declared = list(load_feature(key).SPEC.columns)
        except (ImportError, ValueError, AttributeError):
            declared = []
        columns = [c for c in declared if c in present] or sorted(present)
        tables[key] = [dict(r, available_epoch_ms=r.get("available_epoch_ms", r["end_epoch_ms"]),
                            coverage=r.get("coverage", 1.0)) for r in rows]
        manifests[key] = dict(columns=columns, rows=len(rows))
    rows = build_decision_rows(tables, manifests, progress=progress, cancelled=cancelled)
    result = predict_rows(rows, request["model"])
    result["coverage"] = coverage_summary(rows)
    return result


def _algorithms(_request, **_):
    from .decision.models import ALGORITHMS

    return ALGORITHMS


def _output_fields(_request, **_):
    from .decision.predict import OUTPUT_FIELDS

    return OUTPUT_FIELDS


def _behavior_catalog(_request, **_):
    from .behavior import catalog

    return catalog()


def _behavior_predict(request, **_):
    from .behavior import predict

    return predict(request["code"], request["source"], request["model_dir"], threshold=request.get("threshold"))


def _behavior_train(request, progress, cancelled):
    from .behavior import train

    return train(request["dataset"], request["output"], codes=request.get("codes"),
                 modality=request.get("modality", "motion"), cache=request.get("cache"), progress=progress)


ACTIONS = {
    "engine.info": _engine_info,
    "features.catalog": _features_catalog,
    "features.extract": _features_extract,
    "decision.algorithms": _algorithms,
    "decision.output_fields": _output_fields,
    "decision.build_dataset": _build_dataset,
    "decision.train": _train,
    "decision.predict_folder": _predict_folder,
    "decision.predict_windows": _predict_windows,
    "behavior.catalog": _behavior_catalog,
    "behavior.predict": _behavior_predict,
    "behavior.train": _behavior_train,
}


def handle(request, *, progress=lambda *_: None, cancelled=lambda: False, raise_errors=False):
    action = (request or {}).get("action")
    envelope = dict(api=ENGINE_API, action=action)
    try:
        if action not in ACTIONS:
            raise ValueError(f"未知操作：{action}；可用操作：{', '.join(sorted(ACTIONS))}")
        result = ACTIONS[action](request, progress=progress, cancelled=cancelled)
        return dict(envelope, ok=True, result=result)
    except Exception as exc:
        if raise_errors:
            raise
        return dict(envelope, ok=False, error=f"{type(exc).__name__}: {exc}",
                    trace=traceback.format_exc(limit=6))
