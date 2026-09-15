"""The common intake for downloader, labeled dataset and imported raw records."""

import hashlib
import json
from pathlib import Path

from cowmata_tailring.workspace.device_identity import resolve_device_identity
from cowmata_tailring.workspace.sensor_records import parse_sensor_object

from .dataset import scan_dataset

EXCLUDED = {".edge-download", "标注工程", ".git", "__pycache__", "Label", ".label-history"}


def scan_inputs(root, *, training=False, progress=lambda *_: None, cancelled=lambda: False):
    root = Path(root).resolve()
    if training:
        result = scan_dataset(
            root, pool_general_events=True, progress=progress, cancelled=cancelled
        )
        if not result["records"]:
            raise ValueError("未找到 Raw/Label 成对训练数据；请在数据集构建中导出已复核标签")
        return result
    if not root.exists():
        raise ValueError("原始数据位置不存在")
    files = [root] if root.is_file() else sorted(root.rglob("*.json"))
    records, issues, seen, owners, conflicts = [], [], set(), {}, set()
    for i, path in enumerate(files):
        if cancelled():
            raise InterruptedError("读取原始数据已取消")
        relative = path.relative_to(root if root.is_dir() else root.parent)
        if (
            path.is_symlink()
            or any(part in EXCLUDED for part in relative.parts)
            or "Temp" in relative.parts
        ):
            continue
        try:
            if path.stat().st_size > 384 * 1024 * 1024:
                raise ValueError("单份记录过大")
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict) or "create_time" not in data:
                continue
            modality = "motion" if "imu" in data else "ppg"
            if modality == "ppg" and not any(
                k in data for k in ("ir_data", "imu_data", "configs", "sample_rate_hz")
            ):
                continue
            sensor = parse_sensor_object(data, path, kind=modality)
            asset = hashlib.sha256(raw).hexdigest()
            identity = resolve_device_identity(path, sensor.device)
            if identity["status"] != "ready":
                # Exported Raw filenames retain the same identity before the timestamp.
                from cowmata_tailring.workspace.device_identity import parse_device_folder

                try:
                    owner = parse_device_folder(path.stem.split("_", 1)[0])
                    if owner.device_id != sensor.device.upper():
                        raise ValueError("文件名与记录设备不一致")
                    identity = dict(
                        cow_id=owner.cow_id, device_id=owner.device_id, field_mark=owner.field_mark
                    )
                except ValueError:
                    raise ValueError(identity["message"]) from None
            key = (identity["device_id"], identity["cow_id"], identity["field_mark"])
            if asset in owners and owners[asset] != key:
                conflicts.add(asset)
                raise ValueError(
                    "相同原始记录出现在不同牛号或现场标号目录；请核对 CSV 修订后的旧副本"
                )
            if asset in seen:
                continue
            owners[asset] = key
            seen.add(asset)
            records.append(
                dict(
                    asset_id=asset,
                    cow_id=identity["cow_id"],
                    device_id=identity["device_id"],
                    field_mark=identity["field_mark"],
                    modality=modality,
                    raw=str(path),
                    events=[],
                    review_coverage=[],
                    duration_ms=sensor.duration_ms,
                    start_epoch_ms=sensor.epoch_at(0),
                    identity_eligible=True,
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(dict(path=str(path), reason=str(exc)))
        progress(i + 1, len(files), "读取九轴与 PPG 原始记录")
    records = [r for r in records if r["asset_id"] not in conflicts]
    fingerprint = hashlib.sha256(
        json.dumps(
            sorted((r["asset_id"], r["device_id"], r["cow_id"], r["field_mark"]) for r in records)
        ).encode()
    ).hexdigest()
    return dict(
        schema="algorithm-inputs-3.9",
        root=str(root),
        fingerprint=fingerprint,
        records=records,
        issues=issues,
        summary=dict(unique_records=len(records), cow_count=len({r["cow_id"] for r in records})),
    )
