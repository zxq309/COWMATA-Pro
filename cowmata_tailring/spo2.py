"""One quality-gated SpO2 record per PPG JSON; original PPG bytes stay immutable.

Mirrors :mod:`cowmata_tailring.temperature`: every derived record is a small JSON
under ``SpO2/<day>/<device-cow-mark>/<stamp>.json`` whose ``data`` field is a single
percentage and whose ``_spo2`` block keeps ratio, heart rate, perfusion, quality
and the source hash, so decisions can always be traced back to the raw waveform.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .algorithms.spo2_signal import CALIBRATION, VERSION

CONTRACT = {"schema": "cowmata-spo2-1", "unit": "percent", "value_field": "data",
            "timestamp_unit": "unix_ms", "algorithm": VERSION,
            "calibration": CALIBRATION["name"]}
CHINA = timezone(timedelta(hours=8))
QUALITIES = ("good", "fair")
HOUR_MS = 3_600_000


def ppg_timing(document, *, maximum_upload_lag_ms=3 * HOUR_MS):
    """(sample_start_ms, available_ms, basis) of one PPG capture.

    Firmware work_mode 6 stamps ``time`` at the start of the capture and uploads about
    an hour later (``create_time``); older files carry ``time == create_time``. Same rule
    as the heart-rate module so both PPG features share one time axis.
    """
    create = document.get("create_time")
    if isinstance(create, bool) or not isinstance(create, int) or create <= 0:
        raise ValueError("PPG create_time 必须是 Unix 毫秒整数")
    update = document.get("update_time")
    available = max(create, update) if isinstance(update, int) and not isinstance(update, bool) and update > 0 else create
    device_time = document.get("time")
    if isinstance(device_time, int) and not isinstance(device_time, bool) and 0 < create - device_time <= maximum_upload_lag_ms:
        return device_time, available, "device_time"
    return create, available, "create_time_upper_bound"


def validate_contract(value):
    if value is None:
        return
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in CONTRACT.items()):
        raise ValueError("血氧格式不兼容：需要 cowmata-spo2-1 / " + VERSION + " 血氧协议")


def read_spo2_record(obj):
    if not isinstance(obj, dict):
        raise ValueError("血氧 JSON 顶层必须是对象")
    value = obj.get("data")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("血氧 data 必须是有限的单个百分比数值")
    if not 0 < value <= 100:
        raise ValueError("血氧百分比超出 0–100")
    stamp = obj.get("create_time")
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("血氧 create_time 必须是 Unix 毫秒整数")
    meta = obj.get("_spo2")
    if not isinstance(meta, dict) or meta.get("schema") != CONTRACT["schema"] or meta.get("unit") != "percent":
        raise ValueError("血氧协议版本或单位不兼容")
    available = obj.get("update_time")
    if available is None:
        available = stamp
    if type(available) is not int or available <= 0:
        raise ValueError("血氧 update_time 必须是 Unix 毫秒整数或空值")

    def number(key):
        item = meta.get(key)
        return float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) else None

    return dict(value=float(value), time=stamp, available_at_ms=max(available, stamp),
                ratio_r=number("ratio_r"), ratio_r_iqr=number("ratio_r_iqr"),
                heart_rate_bpm=number("heart_rate_bpm"),
                perfusion_index_percent=number("perfusion_index_percent"),
                windows_valid=int(meta.get("windows_valid") or 0),
                windows_total=int(meta.get("windows_total") or 0),
                quality=meta.get("quality"), algorithm=meta.get("algorithm"),
                source_sha256=meta.get("source_sha256"), sample_id=meta.get("sample_id"),
                duration_ms=number("duration_ms"), time_basis=meta.get("time_basis"))


def analyse_ppg_document(obj, source_path):
    from .algorithms.spo2_signal import analyse_ppg
    from .workspace.sensor_records import parse_ppg_object
    ppg = parse_ppg_object(obj, source_path)
    return ppg, analyse_ppg(ppg)


def ppg_spo2_record(obj, *, source_path, source_sha256):
    """Return ``(record_or_None, analysis)``; record is None when SpO2 is not trustworthy."""
    ppg, analysis = analyse_ppg_document(obj, source_path)
    config = obj.get("configs") or {}
    if isinstance(config, str):
        config = json.loads(config)
    if analysis["spo2_percent"] is None or analysis["quality"] not in QUALITIES:
        return None, analysis
    stamp, update, basis = ppg_timing(obj)
    update = max(update, stamp + int(round(ppg.duration_ms)))
    record = dict(configs=None, cow_id=obj.get("cow_id") or "", create_by=obj.get("create_by"),
        create_time=stamp, data=analysis["spo2_percent"], device=str(obj.get("device", "")).upper(),
        log=None, uid=f"ppg:{source_sha256}", update_by=obj.get("update_by"), update_time=update,
        vbat=obj.get("vbat"),
        _spo2=dict(schema=CONTRACT["schema"], unit="percent", source_kind="ppg_red_ir_record",
            time_basis=basis, algorithm=VERSION, calibration=CALIBRATION["name"],
            source_sha256=source_sha256, source_uid=obj.get("uid"), source_create_time=obj.get("create_time"),
            device_time=obj.get("time"), duration_ms=ppg.duration_ms, sample_rate_hz=ppg.sample_rate_hz,
            pulse_led_mode=config.get("pulse_led_mode"), ratio_r=analysis["ratio_r"],
            ratio_r_iqr=analysis["ratio_r_iqr"], heart_rate_bpm=analysis["heart_rate_bpm"],
            perfusion_index_percent=analysis["perfusion_index_percent"], quality=analysis["quality"],
            windows_valid=analysis["windows_valid"], windows_pulse=analysis["windows_pulse"],
            windows_total=analysis["windows_total"], red_dc=analysis["red_dc"], ir_dc=analysis["ir_dc"],
            sample_id=f"{source_sha256}:spo2"))
    return record, analysis


def spo2_owner(source, obj, explicit=None):
    from .temperature import temperature_owner
    return temperature_owner(Path(source), obj, explicit)


def save_spo2_record(root, folder, obj):
    sample = read_spo2_record(obj)
    root = Path(root).resolve()
    if not folder or Path(folder).name != folder:
        raise ValueError("血氧设备目录不合法")
    stamp = datetime.fromtimestamp(sample["time"] / 1000, CHINA)
    destination = root / "SpO2" / stamp.strftime("%Y-%m-%d") / folder / (stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".json")
    if not destination.resolve().is_relative_to(root):
        raise ValueError("血氧路径越界")
    if destination.exists():
        previous = json.loads(destination.read_text(encoding="utf-8-sig"))
        old = read_spo2_record(previous)
        if (old["time"], old["value"], old["source_sha256"]) == (sample["time"], sample["value"], sample["source_sha256"]):
            return destination, False
        raise ValueError("同秒血氧内容冲突，原件保留：" + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".spo2-", suffix=".partial", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination, True


def export_spo2_sources(sources, root, *, before_ms=None, cancelled=lambda: False,
                        progress=lambda *_: None, audit=None):
    """Derive SpO2 records from PPG JSON. ``audit`` (list) receives one row per source."""
    result = dict(contract=dict(CONTRACT), source_records=0, samples=0, created=0, reused=0,
                  rejected=0, rejected_reasons={}, issues=[])
    seen = set()
    for i, entry in enumerate(sources):
        if cancelled():
            raise InterruptedError("血氧提取已暂停；原始数据保留")
        entry = {"path": str(entry)} if not isinstance(entry, dict) else entry
        source = Path(entry["path"])
        row = dict(source=str(source), status="", target="", spo2_percent=None, quality="", reason="")
        try:
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            obj = json.loads(content.decode("utf-8-sig"))
            if not isinstance(obj, dict) or "imu" in obj or not obj.get("ir_data"):
                row.update(status="skipped", reason="不是红光/红外 PPG 记录")
                continue
            if before_ms is not None and int(obj.get("create_time", 0)) >= before_ms:
                continue
            folder = spo2_owner(source, obj, entry.get("device_folder"))
            if (digest, folder) in seen:
                row.update(status="duplicate", reason="同一原始记录已处理")
                continue
            seen.add((digest, folder))
            result["source_records"] += 1
            record, analysis = ppg_spo2_record(obj, source_path=source, source_sha256=digest)
            row.update(owner=folder, sha256=digest, create_time=obj.get("create_time"),
                       heart_rate_bpm=analysis["heart_rate_bpm"], ratio_r=analysis["ratio_r"],
                       perfusion_index_percent=analysis["perfusion_index_percent"],
                       windows_valid=analysis["windows_valid"], windows_total=analysis["windows_total"],
                       quality=analysis["quality"], spo2_percent=analysis["spo2_percent"])
            if record is None:
                reason = max(analysis["reject_reasons"], key=analysis["reject_reasons"].get) if analysis["reject_reasons"] else analysis["quality"]
                result["rejected"] += 1
                result["rejected_reasons"][reason] = result["rejected_reasons"].get(reason, 0) + 1
                row.update(status="rejected", reason=reason)
                continue
            destination, created = save_spo2_record(root, folder, record)
            result["samples"] += 1
            result["created" if created else "reused"] += 1
            row.update(status="created" if created else "reused", target=str(destination))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["issues"].append(dict(path=str(source), reason=str(exc)))
            row.update(status="error", reason=str(exc))
        finally:
            if audit is not None and row["status"]:
                audit.append(row)
            progress(i + 1, len(sources), "由 PPG 计算血氧")
    return result


def find_spo2_sources(sources):
    """PPG JSON below any ``PPG`` folder; hidden folders and annotations are pruned."""
    found, seen = [], set()
    for source in sources:
        source = Path(source).resolve()
        entries = [(str(source.parent), [], [source.name])] if source.is_file() else os.walk(source, followlinks=False)
        for directory, directories, names in entries:
            base = Path(directory)
            directories[:] = sorted(name for name in directories if not name.startswith(".") and name not in {"标注工程", "Label", "SpO2", "Temp"}
                                    and not (base / name).is_symlink())
            parts = {p.casefold() for p in base.parts}
            if "ppg" not in parts:
                continue
            for name in sorted(names):
                file = base / name
                if file.suffix.lower() != ".json" or "标注" in name or name.endswith("_label.json"):
                    continue
                key = os.path.normcase(str(file))
                if key not in seen:
                    seen.add(key)
                    found.append(file)
    return found


def external_spo2(root):
    """Read derived SpO2 records under ``root`` (any ``SpO2`` folder)."""
    rows, issues = [], []
    root = Path(root)
    for file in sorted(root.rglob("*.json")):
        relative = file.relative_to(root)
        if "spo2" not in {p.casefold() for p in relative.parts} or any(p.startswith(".") for p in relative.parts):
            continue
        try:
            from .workspace.device_identity import parse_device_folder
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            owner = parse_device_folder(file.parent.name)
            if owner.device_id != str(data.get("device", "")).upper():
                raise ValueError("血氧目录与设备不一致")
            sample = read_spo2_record(data)
            rows.append(dict(cow=owner.cow_id, device=owner.device_id, mark=owner.field_mark,
                             source=str(file), **sample))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append(dict(path=str(file), reason=str(exc)))
    return rows, issues
