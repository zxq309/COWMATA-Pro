"""Decision dataset: align the seven feature streams per cow and derive causal trends.

Pipeline
    raw JSON folder ──(feature plug-ins, parallel)──► Features/<key>/windows.csv
    windows ──(per cow, 10 min grid)──► hourly decision rows with derived trends
    decision rows + calving truth ──► labelled table for training / evaluation

Every derived value at decision time ``t`` only uses windows whose value was available at
``t`` (``available_epoch_ms <= t``), approximated per feature by a constant delay rounded up to
the 10 min grid. Calving truth is attached after derivation and never read by features.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import time
import warnings
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from cowmata_engine.features import FEATURE_MODULES, WINDOW_MS, load_feature, validate_rows

HOUR_MS = 3_600_000
SLOTS_PER_HOUR = HOUR_MS // WINDOW_MS  # 6
DERIVATIONS = {
    "1h": "近 1 小时均值",
    "6h": "近 6 小时均值",
    "d24": "近 6 小时相对本牛前 24 小时中位数的变化",
    "z72": "近 6 小时相对本牛前 72 小时的稳健 z 分数",
    "slope6h": "近 6 小时斜率（每小时）",
    "circ": "近 1 小时相对前 3 天同一时段的日节律残差",
}
HORIZONS = (6, 12, 24, 48)
IDENTITY = ("cow_id", "device_id", "field_mark")
EXCLUDED_DIRS = {".edge-download", "标注工程", ".git", "__pycache__", "Label", ".label-history",
                 "归类附属文件", "科牧特_协作标注"}
CHINA_OFFSET_H = 8
TABLE_SCHEMA = "cowmata-decision-table-4.3.3"


# ----------------------------------------------------------------------------- scanning

def _modality(path: Path) -> str | None:
    parts = {p.casefold() for p in path.parts}
    if "motion" in parts or "九轴" in parts:
        return "motion"
    if "ppg" in parts:
        return "ppg"
    if "temp" in parts:
        return "temp"
    try:
        with path.open("rb") as stream:
            head = stream.read(4096).decode("utf-8", "ignore")
    except OSError:
        return None
    if '"imu"' in head:
        return "motion"
    if '"ir_data"' in head or '"configs"' in head:
        return "ppg"
    return "temp" if '"create_time"' in head else None


def _identity(path: Path):
    from cowmata_tailring.workspace.device_identity import parse_device_folder, source_device_folder

    folder = source_device_folder(path)
    candidates = [folder.name] if folder is not None else []
    candidates.append(path.stem.split("_", 1)[0])
    for name in candidates:
        try:
            owner = parse_device_folder(name)
            return owner.cow_id, owner.device_id, owner.field_mark
        except ValueError:
            continue
    return None


def scan_sources(root, *, progress=lambda *_: None, cancelled=lambda: False):
    """Group raw JSON files by (cow, device, mark, modality); identity from folder names only."""
    root = Path(root).resolve()
    if not root.exists():
        raise ValueError("原始数据目录不存在")
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*.json")
        if not any(part in EXCLUDED_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1])
        and not p.stem.endswith((".标注", "_label")))
    groups, issues = defaultdict(list), []
    for number, path in enumerate(files, 1):
        if cancelled():
            raise InterruptedError("读取原始数据已取消")
        modality = _modality(path)
        identity = _identity(path)
        if modality is None:
            issues.append(dict(path=str(path), reason="无法判断数据类型"))
        elif identity is None:
            issues.append(dict(path=str(path), reason="目录或文件名缺少 设备-耳标-记号 身份"))
        else:
            groups[(*identity, modality)].append(str(path))
        if number % 200 == 0 or number == len(files):
            progress(number, len(files), "扫描原始记录")
    return {key: sorted(paths, key=lambda p: Path(p).name) for key, paths in groups.items()}, issues


# ----------------------------------------------------------------------------- extraction

def _extract_task(key, identity, paths, window_ms):
    module = load_feature(key)
    spec = module.SPEC
    rows, issues = [], []
    if hasattr(module, "extract_series"):
        try:
            rows = list(module.extract_series(list(paths), window_ms=window_ms))
        except Exception as exc:  # plug-in failures stay per group
            issues.append(dict(path=paths[0] if paths else "", feature=key, reason=f"{type(exc).__name__}: {exc}"))
    else:
        for path in paths:
            try:
                produced = list(module.extract(path, window_ms=window_ms))
                for row in produced:
                    row.setdefault("source", path)
                rows.extend(produced)
            except Exception as exc:
                issues.append(dict(path=path, feature=key, reason=f"{type(exc).__name__}: {exc}"))
    try:
        validate_rows(spec, rows)
    except ValueError as exc:
        return [], issues + [dict(path=paths[0] if paths else "", feature=key, reason=str(exc))]
    cow, device, mark = identity
    for row in rows:
        row.update(cow_id=cow, device_id=device, field_mark=mark)
        row.setdefault("source", paths[0] if paths else "")
    return rows, issues


def extract_feature_tables(root, *, keys=None, workers=None, window_ms=WINDOW_MS, output=None,
                           progress=lambda *_: None, cancelled=lambda: False):
    """Run every available feature plug-in over a raw folder. Returns {key: rows}, issues, catalog."""
    groups, issues = scan_sources(root, progress=progress, cancelled=cancelled)
    ready, catalog = {}, []
    for key in keys or FEATURE_MODULES:
        try:
            module = load_feature(key)
            ready[key] = module.SPEC
            catalog.append(dict(ready=True, **module.SPEC.as_dict()))
        except (ImportError, ValueError, AttributeError) as exc:
            catalog.append(dict(key=key, ready=False, error=str(exc)))
    tasks = [(key, group[:3], paths) for key, spec in ready.items()
             for group, paths in groups.items() if group[3] == spec.modality]
    tables = {key: [] for key in ready}
    if tasks:
        count = max(1, min(workers or os.cpu_count() or 2, 16, len(tasks)))
        done = 0
        if count == 1:
            for key, identity, paths in tasks:
                if cancelled():
                    raise InterruptedError("特征提取已取消")
                rows, errors = _extract_task(key, identity, paths, window_ms)
                tables[key].extend(rows)
                issues.extend(errors)
                done += 1
                progress(done, len(tasks), "计算决策特征")
        else:
            with ProcessPoolExecutor(max_workers=count) as pool:
                futures = {pool.submit(_extract_task, key, identity, paths, window_ms): key
                           for key, identity, paths in tasks}
                for future in as_completed(futures):
                    if cancelled():
                        for pending in futures:
                            pending.cancel()
                        raise InterruptedError("特征提取已取消")
                    rows, errors = future.result()
                    tables[futures[future]].extend(rows)
                    issues.extend(errors)
                    done += 1
                    progress(done, len(tasks), "计算决策特征")
    if output is not None:
        write_feature_tables(output, tables, ready, root=root)
    return tables, issues, catalog


def write_feature_tables(output, tables, specs, *, root=None):
    output = Path(output)
    for key, rows in tables.items():
        spec = specs[key]
        folder = output / key
        folder.mkdir(parents=True, exist_ok=True)
        columns = ["cow_id", "device_id", "field_mark", "start_epoch_ms", "end_epoch_ms",
                   "available_epoch_ms", "coverage", *spec.columns, "source"]
        rows = sorted(rows, key=lambda r: (r["cow_id"], r["device_id"], r["start_epoch_ms"]))
        with (folder / "windows.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                if root is not None and row.get("source"):
                    try:
                        row = {**row, "source": Path(row["source"]).resolve().relative_to(Path(root).resolve()).as_posix()}
                    except ValueError:
                        pass
                writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
        (folder / "feature-manifest.json").write_text(json.dumps(dict(
            key=key, version=spec.version, api="cowmata-decision-feature-1", columns=list(spec.columns),
            rows=len(rows), cows=len({r["cow_id"] for r in rows}),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            generated_by="cowmata_engine.decision.dataset"), ensure_ascii=False, indent=2), encoding="utf-8")


def _number(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _feature_file(features_root, key):
    """``<root>/<key>/windows.csv``; ``features_root`` may also be ``{key: folder}``."""
    if isinstance(features_root, dict):
        folder = features_root.get(key)
        if not folder:
            return None
        folder = Path(folder)
        for candidate in (folder / "windows.csv", folder / key / "windows.csv"):
            if candidate.is_file():
                return candidate
        return None
    candidate = Path(features_root) / key / "windows.csv"
    return candidate if candidate.is_file() else None


def load_feature_tables(features_root, keys=None):
    """Read ``<root>/<key>/windows.csv`` produced by the feature sessions or by the engine."""
    tables, manifests = {}, {}
    for key in keys or FEATURE_MODULES:
        file = _feature_file(features_root, key)
        if file is None:
            continue
        manifest = file.parent / "feature-manifest.json"
        doc = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        with file.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = doc.get("columns") or [c for c in reader.fieldnames or [] if c not in (
                *IDENTITY, "start_epoch_ms", "end_epoch_ms", "available_epoch_ms", "coverage", "source")]
            rows = []
            for raw in reader:
                start, end = _number(raw.get("start_epoch_ms")), _number(raw.get("end_epoch_ms"))
                if start is None or end is None or not raw.get("cow_id"):
                    continue
                available = _number(raw.get("available_epoch_ms"))
                row = dict(cow_id=raw["cow_id"].strip(), device_id=(raw.get("device_id") or "").strip(),
                           field_mark=(raw.get("field_mark") or "").strip(), start_epoch_ms=int(start),
                           end_epoch_ms=int(end), available_epoch_ms=int(available if available is not None else end),
                           coverage=_number(raw.get("coverage")) or 0.0, source=raw.get("source", ""))
                row.update({c: _number(raw.get(c)) for c in columns})
                rows.append(row)
        tables[key] = rows
        manifests[key] = dict(doc, columns=list(columns), rows=len(rows), file=str(file))
        try:
            spec = load_feature(key).SPEC
            manifests[key].setdefault("version", spec.version)
            manifests[key].setdefault("primary", spec.primary)
            if spec.derivations:
                manifests[key]["derivations"] = list(spec.derivations)
        except (ImportError, ValueError, AttributeError):
            pass
    return tables, manifests


# ----------------------------------------------------------------------------- derivation

def _rolling(values, n, fn, min_periods):
    import pandas as pd

    series = pd.Series(values)
    roll = series.rolling(n, min_periods=min_periods)
    return getattr(roll, fn)().to_numpy() if isinstance(fn, str) else fn(roll).to_numpy()


def _shift(values, n):
    out = np.full_like(values, np.nan)
    if n < len(values):
        out[n:] = values[:len(values) - n]
    return out


def _rolling_slope(values, n):
    """Least-squares slope per slot over the last ``n`` slots, NaN-aware and vectorised."""
    valid = np.isfinite(values).astype(float)
    y = np.where(valid > 0, values, 0.0)
    x = np.arange(len(values), dtype=float) - len(values) / 2  # centred for numerical stability

    def rsum(a):
        c = np.cumsum(np.r_[0.0, a])
        out = np.full(len(a), np.nan)
        if len(a) >= n:
            out[n - 1:] = c[n:] - c[:-n]
        return out

    k, sx, sy = rsum(valid), rsum(x * valid), rsum(y)
    sxx, sxy = rsum(x * x * valid), rsum(x * y)
    den = k * sxx - sx * sx
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = (k * sxy - sx * sy) / den
    slope[(k < max(3, n // 3)) | ~np.isfinite(slope)] = np.nan
    return slope


def derive_series(values, coverage):
    """All derivations on one regular 10 min series (index = slot, value at slot end)."""
    v = np.asarray(values, dtype=float)
    six, day, three = 6 * SLOTS_PER_HOUR, 24 * SLOTS_PER_HOUR, 72 * SLOTS_PER_HOUR
    m1 = _rolling(v, SLOTS_PER_HOUR, "mean", 1)
    m6 = _rolling(v, six, "mean", SLOTS_PER_HOUR)
    base24 = _shift(_rolling(v, day, "median", six), six)
    base72 = _shift(_rolling(v, three, "median", day), six)
    q75 = _shift(_rolling(v, three, lambda r: r.quantile(0.75), day), six)
    q25 = _shift(_rolling(v, three, lambda r: r.quantile(0.25), day), six)
    scale = np.maximum((q75 - q25) / 1.349, 1e-3 * (np.abs(base72) + 1.0))
    with np.errstate(invalid="ignore", divide="ignore"):
        z72 = np.clip((m6 - base72) / scale, -20, 20)
    same = np.vstack([_shift(m1, k * day) for k in (1, 2, 3)])
    enough = np.sum(np.isfinite(same), axis=0) >= 2
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        circ = np.where(enough, m1 - np.nanmedian(same, axis=0), np.nan)
    slope = _rolling_slope(v, six) * SLOTS_PER_HOUR
    cov6 = _rolling(np.nan_to_num(np.asarray(coverage, dtype=float)), six, "mean", 1)
    return {"1h": m1, "6h": m6, "d24": m6 - base24, "z72": z72, "slope6h": slope, "circ": circ}, cov6


def _grid(rows, columns, origin, count):
    """Coverage-weighted mean per 10 min slot; returns (values[col], coverage, delay_slots)."""
    values = {c: np.zeros(count) for c in columns}
    weights = {c: np.zeros(count) for c in columns}
    coverage = np.zeros(count)
    delay = 0
    for row in rows:
        slot = int((row["start_epoch_ms"] - origin) // WINDOW_MS)
        if not 0 <= slot < count:
            continue
        w = max(float(row.get("coverage") or 0.0), 1e-3)
        coverage[slot] = max(coverage[slot], float(row.get("coverage") or 0.0))
        delay = max(delay, int(row.get("available_epoch_ms", row["end_epoch_ms"])) - int(row["end_epoch_ms"]))
        for c in columns:
            x = row.get(c)
            if x is not None and math.isfinite(x):
                values[c][slot] += w * x
                weights[c][slot] += w
    out = {}
    for c in columns:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[c] = np.where(weights[c] > 0, values[c] / np.maximum(weights[c], 1e-12), np.nan)
    return out, coverage, int(math.ceil(delay / WINDOW_MS))


def feature_columns(manifests):
    """Base columns per key, prefixed with the key when two plug-ins collide."""
    seen, result = defaultdict(int), {}
    for doc in manifests.values():
        for c in doc["columns"]:
            seen[c] += 1
    for key, doc in manifests.items():
        result[key] = [(c, c if seen[c] == 1 else f"{key}.{c}") for c in doc["columns"]]
    return result


def build_decision_rows(tables, manifests, *, step_ms=HOUR_MS, min_history_h=0.0,
                        progress=lambda *_: None, cancelled=lambda: False):
    """Hourly decision rows per cow with all causal derivations. Truth is not attached here."""
    names = feature_columns(manifests)
    all_derived = [f"{name}@{deriv}" for key, pairs in names.items() for _, name in pairs
                   for deriv in DERIVATIONS if deriv in set(manifests[key].get("derivations") or DERIVATIONS)]
    by_cow = defaultdict(lambda: defaultdict(list))
    for key, rows in tables.items():
        for row in rows:
            by_cow[row["cow_id"]][key].append(row)
    output = []
    cows = sorted(by_cow)
    for number, cow in enumerate(cows, 1):
        if cancelled():
            raise InterruptedError("决策数据集构建已取消")
        streams = by_cow[cow]
        first = min(r["start_epoch_ms"] for rows in streams.values() for r in rows)
        last = max(r["end_epoch_ms"] for rows in streams.values() for r in rows)
        origin = (first // WINDOW_MS) * WINDOW_MS
        count = int((last - origin) // WINDOW_MS) + 1
        derived, cover, delays = {}, {}, {}
        for key, rows in streams.items():
            base, coverage, delay = _grid(rows, [c for c, _ in names[key]], origin, count)
            delays[key] = delay
            cover[key] = None
            allowed = set(manifests[key].get("derivations") or DERIVATIONS)
            for column, name in names[key]:
                parts, cov6 = derive_series(base[column], coverage)
                cover[key] = cov6
                for deriv, series in parts.items():
                    if deriv in allowed:
                        derived[f"{name}@{deriv}"] = (key, series)
        devices = sorted({r["device_id"] for rows in streams.values() for r in rows})
        start_t = int(math.ceil((origin + WINDOW_MS) / step_ms) * step_ms)
        for t in range(start_t, int(last) + step_ms, step_ms):
            row = dict(cow_id=cow, devices=";".join(devices), decision_epoch_ms=int(t),
                       history_hours=round((t - first) / HOUR_MS, 3))
            local = (t / HOUR_MS + CHINA_OFFSET_H) % 24
            row["hour_sin"], row["hour_cos"] = math.sin(2 * math.pi * local / 24), math.cos(2 * math.pi * local / 24)
            row.update(dict.fromkeys(all_derived))
            present = 0
            for key in FEATURE_MODULES:
                if key not in streams:
                    row[f"coverage.{key}@6h"] = 0.0
                    continue
                slot = int((t - origin) // WINDOW_MS) - 1 - delays[key]
                value = float(cover[key][slot]) if 0 <= slot < count else 0.0
                row[f"coverage.{key}@6h"] = value
                present += value > 0
            for name, (key, series) in derived.items():
                slot = int((t - origin) // WINDOW_MS) - 1 - delays[key]
                x = series[slot] if 0 <= slot < count else np.nan
                row[name] = float(x) if np.isfinite(x) else None
            row["features_present"] = present
            if present and row["history_hours"] >= min_history_h:
                output.append(row)
        progress(number, len(cows), "按牛对齐特征并派生趋势")
    return output


def attach_truth(rows, calvings, *, horizons=HORIZONS, lookback_days=10):
    from .labels import label_row

    for row in rows:
        found = label_row(calvings.get(row["cow_id"]), row["decision_epoch_ms"], lookback_days=lookback_days)
        if found is None:
            row.update(hours_to_calving=None, calving_epoch_ms=None, label_source=None)
            for h in horizons:
                row[f"y_{h}h"] = None
        else:
            hours, when, source = found
            row.update(hours_to_calving=round(hours, 4), calving_epoch_ms=int(when), label_source=source)
            for h in horizons:
                row[f"y_{h}h"] = int(hours <= h)
    return rows


def column_key_map(columns, features):
    """{input column: feature key | 'context'} using the per-key base columns of the dataset."""
    owner = {}
    for key, info in (features or {}).items():
        for c in info.get("columns", []):
            owner.setdefault(c, key)
            owner[f"{key}.{c}"] = key
    result = {}
    for column in columns:
        base = column.split("@")[0]
        if base.startswith("coverage."):
            result[column] = base.split(".", 1)[1]
        else:
            result[column] = owner.get(base, "context")
    return result


def model_columns(rows):
    """Numeric input columns (derived features + context), never identity, truth or data-coverage.

    Coverage columns stay in the table for display, but are not model inputs: how much of a feature
    a dataset happened to compute is an availability artefact, not a physiological signal.
    """
    skip = {"cow_id", "devices", "decision_epoch_ms", "hours_to_calving", "calving_epoch_ms",
            "label_source", "history_hours", "features_present"}
    keys = []
    for row in rows[:1]:
        keys = [k for k in row if k not in skip and not k.startswith(("y_", "coverage."))]
    return keys


def profile(rows, *, bins_h=6, span_h=168):
    """Median / IQR of every ``@1h`` column by hours-before-calving (statistical regularity)."""
    labelled = [r for r in rows if r.get("hours_to_calving") is not None and r["hours_to_calving"] <= span_h]
    columns = sorted({k for r in rows[:1] for k in r if k.endswith("@1h")})
    result = {}
    for column in columns:
        points = []
        for lo in range(0, span_h, bins_h):
            values = [r[column] for r in labelled if lo < r["hours_to_calving"] <= lo + bins_h and r.get(column) is not None]
            if len(values) >= 5:
                q = np.percentile(values, [25, 50, 75])
                points.append(dict(hours_before=-(lo + bins_h / 2), q25=float(q[0]), median=float(q[1]),
                                   q75=float(q[2]), n=len(values), cows=len({r["cow_id"] for r in labelled
                                                                               if lo < r["hours_to_calving"] <= lo + bins_h
                                                                               and r.get(column) is not None})))
        result[column.removesuffix("@1h")] = points
    return result


def univariate(rows, horizon=24):
    """AUC of each input column alone for the chosen horizon (direction-free, 0.5–1)."""
    from sklearn.metrics import roc_auc_score

    target = f"y_{horizon}h"
    labelled = [r for r in rows if r.get(target) is not None]
    y = np.asarray([r[target] for r in labelled])
    result = []
    if len(set(y)) < 2:
        return result
    for column in model_columns(labelled):
        x = np.asarray([np.nan if r.get(column) is None else r[column] for r in labelled], dtype=float)
        ok = np.isfinite(x)
        if ok.sum() < 30 or len(set(y[ok])) < 2:
            continue
        auc = float(roc_auc_score(y[ok], x[ok]))
        pos, neg = x[ok & (y == 1)], x[ok & (y == 0)]
        result.append(dict(column=column, auc=max(auc, 1 - auc), direction="升高" if auc >= 0.5 else "降低",
                           coverage=float(ok.mean()), median_positive=float(np.median(pos)) if len(pos) else None,
                           median_negative=float(np.median(neg)) if len(neg) else None))
    return sorted(result, key=lambda r: -r["auc"])


def build_dataset(*, output, features_root=None, raw_root=None, ledger=None, calving_dataset=None,
                  keys=None, workers=None, horizons=HORIZONS, lookback_days=10,
                  progress=lambda *_: None, cancelled=lambda: False):
    """Build (or load) feature windows, derive decision rows and attach calving truth."""
    started = time.monotonic()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    issues, catalog = [], []
    if features_root:
        tables, manifests = load_feature_tables(features_root, keys)
    elif raw_root:
        rows, issues, catalog = extract_feature_tables(raw_root, keys=keys, workers=workers, output=output / "Features",
                                                       progress=progress, cancelled=cancelled)
        tables, manifests = load_feature_tables(output / "Features", keys)
    else:
        raise ValueError("请提供特征数据集目录或原始数据目录")
    if not tables:
        raise ValueError("没有可用的特征窗口；请先让各特征模块生成 windows.csv")
    decision = build_decision_rows(tables, manifests, progress=progress, cancelled=cancelled)
    from .labels import load_calvings

    calvings = load_calvings(ledger, calving_dataset, progress=progress) if (ledger or calving_dataset) else {}
    attach_truth(decision, calvings, horizons=horizons, lookback_days=lookback_days)
    columns = model_columns(decision)
    coverage_columns = [f"coverage.{k}@6h" for k in FEATURE_MODULES]
    table_file = output / "decision_table.csv"
    header = ["cow_id", "devices", "decision_epoch_ms", "history_hours", "features_present",
              "hours_to_calving", "calving_epoch_ms", "label_source", *[f"y_{h}h" for h in horizons],
              *coverage_columns, *columns]
    with table_file.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, header, extrasaction="ignore")
        writer.writeheader()
        for row in decision:
            writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in header})
    labelled = [r for r in decision if r.get("hours_to_calving") is not None]
    summary = dict(
        schema=TABLE_SCHEMA, table=table_file.name, rows=len(decision), labelled_rows=len(labelled),
        cows=len({r["cow_id"] for r in decision}), calving_cows=len({r["cow_id"] for r in labelled}),
        calvings=sum(len(v) for v in calvings.values()),
        label_sources={s: sum(1 for c in calvings.values() for i in c if i["source"] == s) for s in ("video", "ledger")},
        horizons=list(horizons), lookback_days=lookback_days, input_columns=columns,
        derivations=DERIVATIONS, features={k: dict(version=m.get("version"), columns=m["columns"], rows=m["rows"],
                                                   primary=m.get("primary"), derivations=m.get("derivations"))
                                           for k, m in manifests.items()},
        missing_features=[k for k in (keys or FEATURE_MODULES) if k not in manifests],
        feature_coverage={k: float(np.mean([r[f"coverage.{k}@6h"] > 0 for r in decision])) if decision else 0.0
                          for k in FEATURE_MODULES},
        positives={f"{h}h": sum(1 for r in labelled if r.get(f"y_{h}h") == 1) for h in horizons},
        profile=profile(decision), univariate=univariate(decision)[:60],
        catalog=catalog, issues=issues[:2000], issue_count=len(issues),
        calving_times={cow: items for cow, items in calvings.items()},
        elapsed_seconds=round(time.monotonic() - started, 2),
        fingerprint=hashlib.sha256(table_file.read_bytes()).hexdigest(),
    )
    (output / "decision-dataset.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def read_table(path):
    """Load ``decision_table.csv`` back into typed row dicts."""
    path = Path(path)
    if path.is_dir():
        path = path / "decision_table.csv"
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            row = {}
            for k, v in raw.items():
                if k in ("cow_id", "devices", "label_source"):
                    row[k] = v or None
                else:
                    row[k] = _number(v)
            row["cow_id"] = raw["cow_id"]
            rows.append(row)
    return rows
