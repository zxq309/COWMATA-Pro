"""Load related signals on their acquisition clock; never resample or copy labels."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from cowmata_tailring.temperature import (
    CHINA,
    read_temperature_record,
    validate_temperature_identity,
)
from cowmata_tailring.ui.widgets import PlotSeries

from .catalog import file_stamp
from .device_identity import DATE_FOLDER, DEVICE, parse_device_folder, resolve_device_identity

KINDS = ("motion", "ppg", "temp")


def split_series(series):
    ppg = any(s.key.startswith("ppg_") for s in series)
    return dict(
        motion=[] if ppg else [s for s in series if s.key != "temperature"],
        ppg=[s for s in series if s.key != "temperature"] if ppg else [],
        temp=[s for s in series if s.key == "temperature"],
    )


def identity_for(path, device):
    path = Path(path)
    if path.parent.name == "Raw" and path.stem.endswith("_raw"):
        value = parse_device_folder(path.stem.split("_", 1)[0])
        if value.device_id != str(device).upper():
            raise ValueError("设备编号冲突")
        return dict(
            status="ready",
            cow_id=value.cow_id,
            device_id=value.device_id,
            field_mark=value.field_mark,
        )
    return resolve_device_identity(path, device)


def scope_for(path, root):
    for parent in Path(path).parents:
        if parent.name.casefold() in {"motion", "ppg", "temp"}:
            return parent.parent
    return Path(root)


def related_files(scope, kind, start, end, identity, cancelled):
    from datetime import datetime

    # Restrict directory work to the current record's days and previous day for
    # cross-midnight packets. Never recursively scan the entire farm on a tab click.
    first = datetime.fromtimestamp(start / 1000, CHINA).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end / 1000, CHINA).date()
    if (last - first).days > 32:
        raise ValueError("单次曲线浏览范围超过 31 天，请选择较短记录")
    modality = next(
        (p for p in scope.iterdir() if p.is_dir() and p.name.casefold() == kind.casefold()), None
    )
    if modality is None:
        return
    day = first
    while day <= last:
        if cancelled():
            raise InterruptedError()
        folder = modality / day.isoformat()
        day += timedelta(days=1)
        if not folder.is_dir():
            continue
        owners = [folder] + list(folder.iterdir())
        for owner in owners:
            if (
                not owner.is_dir()
                or owner.is_symlink()
                or getattr(owner, "is_junction", lambda: False)()
            ):
                continue
            try:
                known = parse_device_folder(owner.name)
            except ValueError:
                known = None
                if owner != folder and owner.name.upper() != identity["device_id"]:
                    continue
            if known and (known.cow_id, known.device_id, known.field_mark) != (
                identity["cow_id"],
                identity["device_id"],
                identity["field_mark"],
            ):
                continue
            for file in owner.iterdir():
                if cancelled():
                    raise InterruptedError()
                if (
                    file.is_file()
                    and file.suffix.lower() == ".json"
                    and not file.is_symlink()
                    and not file.name.endswith(".标注.json")
                ):
                    yield file


def legacy_curve_identity(primary, cow_id):
    """Display legacy same-device samples without granting a cow/label binding."""
    path = Path(primary.source_path)
    owner = path.parent.name
    if not (DEVICE.fullmatch(owner) or DATE_FOLDER.fullmatch(owner)):
        return None
    if not any(p.name.casefold() in KINDS for p in path.parents):
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    device = str(data.get("device", "")).upper()
    if not DEVICE.fullmatch(device) or device != str(primary.device).upper():
        return None
    if DEVICE.fullmatch(owner) and owner.upper() != device:
        return None
    declared = str(data.get("cow_id") or "").strip()
    known = parse_device_folder(device + "-" + declared) if declared else None
    if cow_id and (known is None or known.cow_id != str(cow_id)):
        return None
    return dict(status="display_only", device_id=device, cow_id=known.cow_id if known else "",
                field_mark=known.field_mark if known else "")


def check_related_identity(data, identity, file):
    if str(data.get("device", "")).upper() != identity["device_id"]:
        raise ValueError("JSON 设备与当前对象冲突")
    declared = str(data.get("cow_id") or "").strip()
    if identity["cow_id"]:
        try:
            parse_device_folder(file.parent.name)
        except ValueError:
            if not declared:
                raise ValueError("旧目录 JSON 缺少牛号，不能自动关联已确认牛号") from None
        validate_temperature_identity(data, identity["cow_id"], identity["field_mark"])
    elif declared:
        raise ValueError("当前牛号待核对，未合并带有其他牛号的记录")


def oximetry_note(signals):
    """One-line SpO2 / pulse summary of the PPG records shown in the PPG sheet."""
    from cowmata_tailring.algorithms.spo2_signal import analyse_ppg
    results = []
    for signal in signals:
        try:
            results.append(analyse_ppg(signal))
        except (ValueError, TypeError, KeyError):
            continue
    if not results:
        return ""
    spo2 = [r["spo2_percent"] for r in results if r["spo2_percent"] is not None and r["quality"] in ("good", "fair")]
    hr = [r["heart_rate_bpm"] for r in results if r["heart_rate_bpm"] is not None]
    pi = [r["perfusion_index_percent"] for r in results if r["perfusion_index_percent"] is not None]
    parts = [f"血氧 {float(np.median(spo2)):.1f} %（{len(spo2)}/{len(results)} 份质量合格，厂商曲线未经牛血气标定）"
             if spo2 else f"血氧：{len(results)} 份 PPG 均未通过质量门控（运动/接触不良），不报数值"]
    if hr:
        parts.append(f"脉率 {float(np.median(hr)):.0f} bpm")
    if pi:
        parts.append(f"灌注指数 {float(np.median(pi)):.2f} %")
    return " · ".join(parts) + "；"


def load_related(primary, root, cow_id, cancelled=lambda: False, allowed=None):
    allowed = set(allowed or KINDS)
    oximetry = [primary] if getattr(primary, "kind", "imu") == "ppg" else []
    base = split_series([PlotSeries(**s) for s in primary.plot_series()])
    for key in KINDS:
        if key not in allowed:
            base[key] = []
    result = dict(modalities=base, messages={}, issues=[], sources=[])
    identity = identity_for(primary.source_path, primary.device)
    if not cow_id or identity.get("status") != "ready" or identity.get("cow_id") != str(cow_id):
        identity = legacy_curve_identity(primary, cow_id) or identity
    if identity.get("status") != "display_only" and (not cow_id or identity.get("status") != "ready" or identity.get("cow_id") != str(cow_id)):
        result["messages"] = {k: "牛号或设备绑定待核对，未自动关联其他记录" for k in KINDS}
        if oximetry:
            result["messages"]["ppg"] = oximetry_note(oximetry) + result["messages"]["ppg"]
        return result
    source_obj = json.loads(Path(primary.source_path).read_text(encoding="utf-8-sig"))
    try:
        validate_temperature_identity(source_obj, identity["cow_id"], identity["field_mark"])
    except ValueError:
        result["messages"] = {
            k: "来源 JSON 牛号或现场记号与当前对象冲突，未关联其他记录" for k in KINDS
        }
        return result
    origin = float(primary.epoch_at(0))
    ending = float(primary.epoch_at(primary.duration_ms))
    scope = scope_for(primary.source_path, root)
    own = "ppg" if getattr(primary, "kind", "imu") == "ppg" else "motion"
    values = {}
    temperature = {}
    estimated = False
    observed = {k: 0 for k in KINDS}
    for kind, directory in [("motion", "Motion"), ("ppg", "PPG"), ("temp", "Temp")]:
        if kind not in allowed:
            continue
        if kind == own:
            continue
        for file in related_files(scope, directory, origin, ending, identity, cancelled):
            if cancelled():
                raise InterruptedError()
            try:
                before = file_stamp(file)
                data = json.loads(file.read_text(encoding="utf-8-sig"))
                check_related_identity(data, identity, file)
                if kind == "temp":
                    sample = read_temperature_record(data)
                    observed[kind] += 1
                    when = sample["time"]
                    if not origin <= when <= ending:
                        continue
                    if file_stamp(file) != before:
                        raise ValueError("读取期间文件变化")
                    key = sample.get("sample_id") or (
                        str(data.get("device")),
                        when,
                        sample["value"],
                    )
                    pair = (when - origin, sample["value"])
                    if key in temperature and temperature[key] != pair:
                        raise ValueError("同一温度采样身份存在不同数值")
                    temperature[key] = pair
                    estimated |= sample["time_basis"] == "imu_bucket_midpoint_estimate"
                else:
                    from .sensor_records import parse_sensor_object

                    signal = parse_sensor_object(data, file, kind="ppg" if kind == "ppg" else "imu")
                    observed[kind] += 1
                    if signal.epoch_at(signal.duration_ms) < origin or signal.epoch_at(0) > ending:
                        continue
                    if file_stamp(file) != before:
                        raise ValueError("读取期间文件变化")
                    if kind == "ppg":
                        oximetry.append(signal)
                    for item in signal.plot_series():
                        if item["key"] == "temperature":
                            continue
                        times = np.asarray(item["times_ms"]) + signal.epoch_at(0) - origin
                        mask = (times >= 0) & (times <= primary.duration_ms)
                        if not mask.any():
                            continue
                        values.setdefault((kind, item["key"]), []).append(
                            (item, times[mask], np.asarray(item["values"])[mask])
                        )
                result["sources"].append(dict(path=str(file), stamp=before, kind=kind))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                result["issues"].append(dict(path=str(file), message=str(exc)))
    for (kind, key), pieces in values.items():
        times = np.concatenate([p[1] for p in pieces])
        samples = np.concatenate([p[2] for p in pieces])
        order = np.argsort(times, kind="stable")
        times, samples = times[order], samples[order]
        # Preserve overlap disagreements as a visible gap, never choose a value.
        unique, starts, counts = np.unique(times, return_index=True, return_counts=True)
        output = np.array(
            [
                samples[i] if n == 1 or np.all(samples[i : i + n] == samples[i]) else np.nan
                for i, n in zip(starts, counts)
            ]
        )
        if np.isnan(output).any():
            result["issues"].append(dict(message="重叠信号值冲突；以缺口显示"))
        item = pieces[0][0]
        base[kind].append(
            PlotSeries(key, item["name"], item["unit"], item["color"], unique, output)
        )
    if temperature:
        samples = sorted(set(temperature.values()))
        # Conflicting scalar values at the same timestamp are not averaged.
        grouped = {}
        for t, value in samples:
            grouped.setdefault(t, set()).add(value)
        times = sorted(grouped)
        base["temp"] = [
            PlotSeries(
                "temperature",
                "温度 °C",
                "°C",
                "#d59338",
                np.array(times),
                np.array(
                    [next(iter(grouped[t])) if len(grouped[t]) == 1 else np.nan for t in times]
                ),
            )
        ]
        result["messages"]["temp"] = "独立温度 JSON · 摄氏度" + (
            "；时间为区间中点估计" if estimated else ""
        )
    elif base["temp"]:
        result["messages"]["temp"] = "来源内附温度；时间为区间中点估计"
    for key in KINDS:
        note = "按本次设备采集时钟对齐，保留原始采样与缺口"
        if identity.get("status") == "display_only":
            note = "旧目录：仅按同设备和重叠采样时间查看；牛号绑定待核对，不自动共享标签"
        if not base[key] and observed[key]:
            note = f"找到 {observed[key]} 份同设备记录，但与当前记录采样时段不重叠；保留真实缺口。" + note
        result["messages"].setdefault(key, note)
    if oximetry and "ppg" in allowed:
        result["messages"]["ppg"] = oximetry_note(oximetry) + result["messages"]["ppg"]
    if result["issues"]:
        for key in KINDS:
            result["messages"][key] += f"；{len(result['issues'])} 项来源问题未强行合并"
    return result


class RelatedSignalLoader(QObject):
    ready = Signal(object)

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.generation = 0
        self.cancellation = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="related-signals")
        self.future = None
        self.ready.connect(self.apply)
        self.destroyed.connect(lambda *_: self.close())

    def cancel(self):
        self.generation += 1
        self.cancellation.set()
        if self.future:
            self.future.cancel()

    def close(self):
        self.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def load(self, primary, root, cow_id, allowed=None):
        self.cancel()
        self.cancellation = threading.Event()
        generation = self.generation
        cancel = self.cancellation
        allowed = set(allowed or KINDS)

        def read():
            try:
                result = load_related(primary, root, cow_id, cancel.is_set, allowed=allowed)
            except InterruptedError:
                return
            except Exception as exc:
                result = dict(
                    modalities={
                        key: ([] if key not in allowed else values)
                        for key, values in split_series([PlotSeries(**s) for s in primary.plot_series()]).items()
                    },
                    messages={k: "关联数据读取失败：" + str(exc) for k in KINDS},
                    issues=[str(exc)],
                )
            if not cancel.is_set():
                try:
                    self.ready.emit((generation, result))
                except RuntimeError:
                    pass

        self.future = self.pool.submit(read)

    def apply(self, value):
        generation, result = value
        if generation != self.generation:
            return
        self.panel.related_result = result
        self.panel.update_modalities(result["modalities"], result["messages"])
