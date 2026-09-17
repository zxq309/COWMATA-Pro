"""One scalar Celsius record per Temp JSON; original Motion bytes stay immutable."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTRACT = {"schema": "cowmata-temperature-1", "unit": "celsius", "value_field": "data",
            "timestamp_unit": "unix_ms", "legacy_encoding": "base64_int16_le_x0.01",
            "legacy_time_basis": "imu_bucket_midpoint_estimate"}
CHINA = timezone(timedelta(hours=8))


def validate_contract(value):
    if value is None:  # documented pre-3.9.3 Celsius models remain readable
        return
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in CONTRACT.items()):
        raise ValueError("温度格式不兼容：需要 cowmata-temperature-1 摄氏温度协议")


def read_temperature_record(obj):
    if not isinstance(obj, dict):
        raise ValueError("温度 JSON 顶层必须是对象")
    value = obj.get("data")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("温度 data 必须是有限的单个摄氏温度数值")
    stamp = obj.get("create_time")
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("温度 create_time 必须是 Unix 毫秒整数")
    meta = obj.get("_temperature") or {}
    if not isinstance(meta, dict) or meta.get("unit", "celsius") != "celsius":
        raise ValueError("温度单位必须为摄氏度")
    if meta.get("schema", CONTRACT["schema"]) != CONTRACT["schema"]:
        raise ValueError("温度协议版本不兼容")
    available = obj.get("update_time")
    if available is None:
        available = stamp
    if type(available) is not int or available <= 0:
        raise ValueError("温度 update_time 必须是 Unix 毫秒整数或空值")
    return dict(value=float(value), time=stamp, available_at_ms=available,
                time_basis=meta.get("time_basis", "device_record_time"),
                source_sha256=meta.get("source_sha256"), sample_id=meta.get("sample_id"),
                source_kind=meta.get("source_kind", "standalone_temp"))


def motion_temperature_records(obj, *, source_sha256):
    """Decode every real bucket, without resampling or pretending its time is exact."""
    if not obj.get("temperature"):
        return []
    from .annotation.data import parse_motion_object
    motion = parse_motion_object(obj)
    raw = base64.b64decode("".join(obj["temperature"].split()), validate=True)
    integers = [value[0] for value in struct.iter_unpack("<h", raw)]
    count = len(integers)
    packet_id = hashlib.sha256(json.dumps([motion.device, motion.create_time_ms,
        motion.version, motion.first_frame_elapsed_ms, motion.duration_ms], separators=(",", ":")).encode()+raw).hexdigest()
    rows = []
    for index, (integer, relative) in enumerate(zip(integers, motion.temperature_times_ms)):
        stamp = int(round(motion.epoch_at(float(relative))))
        rows.append(dict(configs=None, cow_id=obj.get("cow_id") or "", create_by=obj.get("create_by"),
            create_time=stamp, data=integer / 100.0, device=motion.device, log=None,
            uid=f"motion:{packet_id}:{index}", update_by=obj.get("update_by"),
            update_time=motion.update_time_ms, vbat=obj.get("vbat"),
            _temperature=dict(schema=CONTRACT["schema"], unit="celsius",
                source_kind="motion_temperature_bucket", time_basis="imu_bucket_midpoint_estimate",
                source_sha256=source_sha256, source_uid=obj.get("uid"),
                source_create_time=motion.create_time_ms, sample_index=index, sample_count=count,
                sample_interval_ms=motion.duration_ms/count, raw_int16=integer,
                sample_id=f"{packet_id}:{index}")))
    return rows


def temperature_owner(source, obj, explicit=None):
    from .workspace.device_identity import parse_device_folder, resolve_device_identity
    if explicit:
        identity = parse_device_folder(explicit)
        if identity.device_id != str(obj.get("device", "")).upper():
            raise ValueError("温度目录与来源设备不一致")
        folder = identity.folder_name
    elif source.parent.name == "Raw" and source.stem.endswith("_raw"):
        identity = parse_device_folder(source.stem.split("_", 1)[0])
        if identity.device_id != str(obj.get("device", "")).upper():
            raise ValueError("数据集名称与来源设备不一致")
        folder = identity.folder_name
    else:
        owner = resolve_device_identity(source, obj.get("device"))
        if owner["status"] != "ready":
            # Retain unresolved identities without inventing a cow number.
            if source.parent.name == str(obj.get("device", "")).upper()+"-待核对":
                return source.parent.name
            raise ValueError(owner["message"])
        identity = parse_device_folder(owner["folder_name"])
        folder = identity.folder_name
    validate_temperature_identity(obj, identity.cow_id, identity.field_mark)
    return folder


def save_temperature_record(root, folder, obj):
    sample = read_temperature_record(obj)
    root = Path(root).resolve()
    if not folder or Path(folder).name != folder:
        raise ValueError("温度设备目录不合法")
    stamp = datetime.fromtimestamp(sample["time"]/1000, CHINA)
    destination = root/"Temp"/stamp.strftime("%Y-%m-%d")/folder/(stamp.strftime("%Y-%m-%d_%H-%M-%S")+".json")
    if not destination.resolve().is_relative_to(root):
        raise ValueError("温度路径越界")
    if destination.exists():
        previous = json.loads(destination.read_text(encoding="utf-8-sig"))
        old = read_temperature_record(previous)
        if (old["time"], old["value"], str(previous.get("device", "")).upper()) == (sample["time"], sample["value"], str(obj.get("device", "")).upper()):
            return destination, False
        raise ValueError("同秒温度内容冲突，原件保留："+str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".temp-", suffix=".partial", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination, True


def export_temperature_sources(sources, root, *, before_ms=None, cancelled=lambda: False,
                               progress=lambda *_: None):
    result = dict(contract=dict(CONTRACT), source_records=0, samples=0, created=0, reused=0, issues=[])
    seen = set()
    for i, entry in enumerate(sources):
        if cancelled():
            raise InterruptedError("温度提取已暂停；原始数据保留")
        entry = {"path":str(entry)} if not isinstance(entry, dict) else entry
        source = Path(entry["path"])
        try:
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            obj = json.loads(content.decode("utf-8-sig"))
            if not isinstance(obj, dict):
                continue
            if before_ms is not None and int(obj.get("create_time", 0)) >= before_ms:
                continue
            if "imu" in obj and not obj.get("temperature"):
                continue
            folder = temperature_owner(source, obj, entry.get("device_folder"))
            if (digest, folder) in seen:
                continue
            seen.add((digest, folder))
            records = motion_temperature_records(obj, source_sha256=digest) if "imu" in obj else [obj]
            result["source_records"] += 1
            for record in records:
                if cancelled():
                    raise InterruptedError("温度提取已暂停；原始数据保留")
                destination, created = save_temperature_record(root, folder, record)
                result["samples"] += 1
                result["created" if created else "reused"] += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["issues"].append(dict(path=str(source), reason=str(exc)))
        progress(i+1, len(sources), "提取独立摄氏温度")
    return result


def find_temperature_sources(sources):
    """Prune hidden files and annotations before enumerating standalone temperature."""
    found = []
    seen = set()
    for source in sources:
        source = Path(source).resolve()
        entries = [(str(source.parent), [], [source.name])] if source.is_file() else os.walk(source, followlinks=False)
        for directory, directories, names in entries:
            base = Path(directory)
            directories[:] = [name for name in directories if not name.startswith('.') and name != '标注工程'
                              and not (base/name).is_symlink() and not (base/name).is_junction()]
            if source.is_file():
                kinds = [p.casefold() for p in reversed(base.parts) if p.casefold() in {'temp', 'motion', 'ppg'}]
                is_temperature = bool(kinds) and kinds[0] == 'temp'
            else:
                is_temperature = 'temp' in {p.casefold() for p in (source.name, *base.relative_to(source).parts)}
            if not is_temperature:
                continue
            for name in sorted(names):
                file = base/name
                if file.suffix.lower() != '.json' or file.is_symlink() or '标注' in name:
                    continue
                key = os.path.normcase(str(file))
                if key not in seen:
                    seen.add(key)
                    found.append(file)
    return found


def validate_temperature_identity(obj, cow_id, field_mark=None):
    declared = str(obj.get('cow_id') or '').strip()
    if not declared or declared == str(cow_id):
        return
    from .workspace.device_identity import parse_device_folder
    try:
        code = parse_device_folder(str(obj.get('device', ''))+'-'+declared)
    except ValueError as exc:
        raise ValueError('温度 JSON 牛号与目录耳标不一致') from exc
    if code.cow_id != str(cow_id) or (field_mark is not None and code.field_mark and code.field_mark != field_mark):
        raise ValueError('温度 JSON 牛号或现场记号与目录不一致')
