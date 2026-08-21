"""Real nine-axis JSON parser used by the native annotation application.

The recorder stores binary values as Base64 strings rather than JSON arrays.
Three IMU frame layouts exist in the collected files:

* v0: 18 bytes -- nine little-endian signed 16-bit values, fixed 20 ms step.
* v1: 20 bytes -- uint16 frame interval followed by the nine values.
* v2: 22 bytes -- uint32 device timestamp followed by the nine values.

Most historical files do not contain a ``version`` member.  They must not all
be treated as v0: genuine 22-byte timestamped files are also present.  This
module therefore combines frame divisibility with a timestamp plausibility
check.  It never silently truncates a damaged binary field.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

GRAVITY_MS2 = 9.80665
DEFAULT_ACCEL_SCALE = 4096
DEFAULT_FIXED_INTERVAL_MS = 20.0

CHANNEL_INFO: tuple[tuple[str, str, str, str], ...] = (
    ("ax", "加速度 X", "m/s²", "#d9534f"),
    ("ay", "加速度 Y", "m/s²", "#299764"),
    ("az", "加速度 Z", "m/s²", "#3979bd"),
    ("gx", "角速度 X", "deg/s", "#d1782f"),
    ("gy", "角速度 Y", "deg/s", "#2b9a91"),
    ("gz", "角速度 Z", "deg/s", "#8462ad"),
    ("mx", "磁场 X", "Gauss", "#c14d79"),
    ("my", "磁场 Y", "Gauss", "#648a35"),
    ("mz", "磁场 Z", "Gauss", "#3181ac"),
    ("temperature", "温度", "°C", "#c46a2c"),
    ("motion", "运动", "次/分钟", "#334155"),
)

_FRAME_BYTES = {0: 18, 1: 20, 2: 22}
_TIMESTAMP_MODES = {
    0: "fixed-20ms",
    1: "frame-delta-ms",
    2: "device-timestamp-ms",
}


class MotionDataError(ValueError):
    """Base exception for an unreadable or invalid motion JSON file."""


class UnsupportedRecordError(MotionDataError):
    """The JSON is valid, but it is not an IMU motion record."""


class InvalidMotionDataError(MotionDataError):
    """The JSON claims to contain IMU data but its payload is malformed."""


@dataclass(slots=True)
class MotionData:
    """Decoded, immutable-by-convention data for one JSON recording."""

    source_path: Path
    uid: int | str | None
    device: str
    version: int
    version_inferred: bool
    frame_bytes: int
    timestamp_mode: str
    acc_scale: int
    create_time_ms: int
    update_time_ms: int | None
    configs: dict[str, Any]
    times_ms: np.ndarray
    channels: dict[str, np.ndarray]
    temperature_times_ms: np.ndarray
    motion_times_ms: np.ndarray
    duration_ms: float
    sample_rate_hz: float
    gap_threshold_ms: float
    gap_indices: np.ndarray
    max_gap_ms: float
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sample_count(self) -> int:
        return int(self.times_ms.size)

    @property
    def samples(self) -> int:
        """Compatibility alias used by early desktop UI revisions."""

        return self.sample_count

    @property
    def gap_count(self) -> int:
        return int(self.gap_indices.size)

    @property
    def temperature(self) -> np.ndarray:
        return self.channels.get("temperature", _empty_f32())

    @property
    def motion(self) -> np.ndarray:
        return self.channels.get("motion", _empty_f32())

    @property
    def create_time(self) -> int:
        return self.create_time_ms

    @property
    def tspan(self) -> float:
        return self.duration_ms

    @property
    def time_ms(self) -> np.ndarray:
        return self.times_ms

    def plot_series(self) -> list[dict[str, Any]]:
        """Return plot descriptions without importing Qt or UI modules."""

        result: list[dict[str, Any]] = []
        for key, name, unit, color in CHANNEL_INFO:
            values = self.channels.get(key)
            if values is None or values.size == 0:
                continue
            if key == "temperature":
                times = self.temperature_times_ms
            elif key == "motion":
                times = self.motion_times_ms
            else:
                times = self.times_ms
            result.append(
                {
                    "key": key,
                    "name": name,
                    "unit": unit,
                    "color": color,
                    "times_ms": times,
                    "values": values,
                }
            )
        return result

    def nearest_sample_index(self, time_ms: float) -> int:
        """Return the nearest IMU sample index for a relative time."""

        if self.sample_count == 0:
            return -1
        pos = int(np.searchsorted(self.times_ms, float(time_ms), side="left"))
        if pos <= 0:
            return 0
        if pos >= self.sample_count:
            return self.sample_count - 1
        before = float(self.times_ms[pos - 1])
        after = float(self.times_ms[pos])
        return pos - 1 if abs(time_ms - before) <= abs(after - time_ms) else pos

    def quality_report(self) -> dict[str, Any]:
        """Return JSON-serializable parsing and timing diagnostics."""

        return {
            "source_path": str(self.source_path),
            "device": self.device,
            "uid": self.uid,
            "version": self.version,
            "version_inferred": self.version_inferred,
            "frame_bytes": self.frame_bytes,
            "timestamp_mode": self.timestamp_mode,
            "sample_count": self.sample_count,
            "duration_ms": round(self.duration_ms, 3),
            "sample_rate_hz": round(self.sample_rate_hz, 6),
            "gap_threshold_ms": round(self.gap_threshold_ms, 3),
            "gap_count": self.gap_count,
            "max_gap_ms": round(self.max_gap_ms, 3),
            "temperature_count": int(self.temperature.size),
            "motion_count": int(self.motion.size),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class SessionData(MotionData):
    """Semantic name for a fully decoded recording session."""


def _empty_f32() -> np.ndarray:
    array = np.empty(0, dtype=np.float32)
    array.setflags(write=False)
    return array


def _empty_f64() -> np.ndarray:
    array = np.empty(0, dtype=np.float64)
    array.setflags(write=False)
    return array


def _readonly(array: np.ndarray) -> np.ndarray:
    array.setflags(write=False)
    return array


def _decode_base64(value: Any, field_name: str, *, required: bool) -> bytes:
    if value is None or value == "":
        if required:
            raise InvalidMotionDataError(f"缺少必需的 {field_name} Base64 字段")
        return b""
    if not isinstance(value, str):
        raise InvalidMotionDataError(f"{field_name} 必须是 Base64 字符串")
    compact = "".join(value.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidMotionDataError(f"{field_name} 不是有效的 Base64：{exc}") from exc


def _parse_configs(value: Any, warnings: list[str]) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            warnings.append("configs 字段不是有效 JSON，已保留原始文本")
            return {"_raw": value}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        warnings.append("configs 内容不是对象，已保留在 _value")
        return {"_value": parsed}
    warnings.append("configs 类型异常，已保留在 _value")
    return {"_value": value}


def _normalise_epoch_ms(value: Any, name: str, *, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            raise InvalidMotionDataError(f"缺少 {name}")
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidMotionDataError(f"{name} 无法解析为整数时间戳") from exc
    # Accept second-resolution Unix timestamps from manually exported files.
    if 1_000_000_000 <= abs(result) < 100_000_000_000:
        result *= 1000
    return result


def _timestamp_score(raw: bytes) -> float:
    """Score whether a 22-byte payload contains plausible uint32 timestamps."""

    if len(raw) < 44 or len(raw) % 22:
        return -1.0
    records = np.frombuffer(
        raw,
        dtype=np.dtype([("timestamp", "<u4"), ("axes", "<i2", (9,))]),
    )
    timestamps = records["timestamp"]
    count = min(timestamps.size, 4097)
    timestamps = timestamps[:count].astype(np.int64, copy=False)
    diffs = np.diff(timestamps)
    if diffs.size == 0:
        return 1.0
    # A single wrap is plausible only at the uint32 boundary.
    wrap = (
        (timestamps[:-1] > 0xF0000000)
        & (timestamps[1:] < 0x0FFFFFFF)
    )
    diffs = np.where(wrap, diffs + 2**32, diffs)
    plausible = (diffs > 0) & (diffs <= 10_000)
    return float(np.count_nonzero(plausible) / plausible.size)


def _delta_score(raw: bytes) -> float:
    """Score whether a 20-byte payload contains plausible uint16 intervals."""

    if len(raw) < 40 or len(raw) % 20:
        return -1.0
    records = np.frombuffer(
        raw,
        dtype=np.dtype([("delta", "<u2"), ("axes", "<i2", (9,))]),
    )
    deltas = records["delta"][1:4097]
    if deltas.size == 0:
        return 1.0
    plausible = (deltas > 0) & (deltas <= 10_000)
    return float(np.count_nonzero(plausible) / plausible.size)


def _choose_version(raw: bytes, declared: Any) -> tuple[int, bool]:
    if declared not in (None, ""):
        try:
            version = int(declared)
        except (TypeError, ValueError) as exc:
            raise InvalidMotionDataError("version 无法解析为整数") from exc
        if version not in _FRAME_BYTES:
            raise InvalidMotionDataError(
                f"不支持 IMU version={version}；当前支持 0、1、2"
            )
        frame_bytes = _FRAME_BYTES[version]
        if len(raw) % frame_bytes:
            raise InvalidMotionDataError(
                f"imu 字节数 {len(raw)} 不能被 v{version} 帧长 "
                f"{frame_bytes} 整除"
            )
        return version, False

    # Timestamp evidence is stronger than divisibility.  This matters for a
    # length divisible by both 18 and 22.
    if len(raw) % 22 == 0 and _timestamp_score(raw) >= 0.90:
        return 2, True
    if len(raw) % 18 == 0:
        return 0, True
    if len(raw) % 20 == 0 and _delta_score(raw) >= 0.90:
        return 1, True
    raise InvalidMotionDataError(
        "缺少 version，且无法可靠推断帧格式："
        f"imu={len(raw)} 字节，除以 18/20/22 的余数分别为 "
        f"{len(raw) % 18}/{len(raw) % 20}/{len(raw) % 22}。"
        "文件可能未写完或二进制内容已损坏。"
    )


def _decode_frames(
    raw: bytes,
    version: int,
    acc_scale: int,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    if version == 0:
        axes_raw = np.frombuffer(raw, dtype="<i2").reshape(-1, 9)
        times_ms = np.arange(axes_raw.shape[0], dtype=np.float64)
        times_ms *= DEFAULT_FIXED_INTERVAL_MS
    elif version == 1:
        records = np.frombuffer(
            raw,
            dtype=np.dtype([("delta", "<u2"), ("axes", "<i2", (9,))]),
        )
        axes_raw = records["axes"]
        times_ms = np.zeros(records.size, dtype=np.float64)
        if records.size > 1:
            times_ms[1:] = np.cumsum(
                records["delta"][1:].astype(np.float64), dtype=np.float64
            )
    else:
        records = np.frombuffer(
            raw,
            dtype=np.dtype([("timestamp", "<u4"), ("axes", "<i2", (9,))]),
        )
        axes_raw = records["axes"]
        timestamps = records["timestamp"].astype(np.int64)
        times_ms = np.zeros(records.size, dtype=np.float64)
        if records.size > 1:
            diffs = np.diff(timestamps)
            wrap = (
                (timestamps[:-1] > 0xF0000000)
                & (timestamps[1:] < 0x0FFFFFFF)
            )
            invalid_reverse = (diffs < 0) & ~wrap
            if np.any(invalid_reverse):
                index = int(np.flatnonzero(invalid_reverse)[0] + 1)
                raise InvalidMotionDataError(
                    f"v2 设备时间戳在样本 {index} 非法倒退，不能保证时间轴真实"
                )
            diffs = np.where(wrap, diffs + 2**32, diffs)
            times_ms[1:] = np.cumsum(diffs, dtype=np.float64)

    if axes_raw.shape[0] == 0:
        raise InvalidMotionDataError("imu 字段没有任何完整样本")
    if times_ms.size > 1 and float(times_ms[-1]) <= 0:
        raise InvalidMotionDataError("IMU 时间戳没有前进，无法建立真实时间轴")

    axes = axes_raw.astype(np.float32, copy=True)
    axes[:, 0:3] *= np.float32(GRAVITY_MS2 / acc_scale)
    axes[:, 3:6] *= np.float32(1.0 / 32.0)
    axes[:, 6:9] *= np.float32(1.0 / 1000.0)
    channels = {
        key: _readonly(axes[:, index])
        for index, key in enumerate(("ax", "ay", "az", "gx", "gy", "gz", "mx", "my", "mz"))
    }
    return _readonly(times_ms), channels


def _decode_bucket(
    raw: bytes,
    *,
    signed: bool,
    scale: float,
    duration_ms: float,
    field_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    if not raw:
        return _empty_f32(), _empty_f64()
    if len(raw) % 2:
        raise InvalidMotionDataError(
            f"{field_name} 字节数 {len(raw)} 不是 16 位整数的完整倍数"
        )
    dtype = "<i2" if signed else "<u2"
    values = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if scale != 1.0:
        values *= np.float32(scale)
    count = values.size
    if count == 1:
        times = np.array([duration_ms * 0.5], dtype=np.float64)
    else:
        step = duration_ms / count if count else 0.0
        times = (np.arange(count, dtype=np.float64) + 0.5) * step
    return _readonly(values), _readonly(times)


def parse_motion_object(
    obj: Mapping[str, Any],
    *,
    source_path: str | Path = "<memory>",
    acc_scale: int = DEFAULT_ACCEL_SCALE,
) -> SessionData:
    """Decode an already loaded motion JSON mapping."""

    if not isinstance(obj, Mapping):
        raise InvalidMotionDataError("JSON 顶层必须是对象")
    if "imu" not in obj or obj.get("imu") in (None, ""):
        kind = "日志/电量记录" if any(k in obj for k in ("log", "vbat", "data")) else "非运动记录"
        raise UnsupportedRecordError(f"该 JSON 是{kind}，不包含 imu 九轴数据")
    if int(acc_scale) <= 0:
        raise ValueError("acc_scale 必须是正整数")
    acc_scale = int(acc_scale)

    warnings: list[str] = []
    imu_raw = _decode_base64(obj.get("imu"), "imu", required=True)
    version, inferred = _choose_version(imu_raw, obj.get("version"))
    if inferred:
        warnings.append(f"JSON 未声明 version，已从二进制内容可靠推断为 v{version}")

    times_ms, channels = _decode_frames(imu_raw, version, acc_scale)
    duration_ms = float(times_ms[-1]) if times_ms.size else 0.0
    temperature, temperature_times = _decode_bucket(
        _decode_base64(obj.get("temperature"), "temperature", required=False),
        signed=True,
        scale=0.01,
        duration_ms=duration_ms,
        field_name="temperature",
    )
    motion, motion_times = _decode_bucket(
        _decode_base64(obj.get("motion"), "motion", required=False),
        signed=False,
        scale=1.0,
        duration_ms=duration_ms,
        field_name="motion",
    )
    if temperature.size:
        channels["temperature"] = temperature
    else:
        warnings.append("JSON 没有温度桶")
    if motion.size:
        channels["motion"] = motion
    else:
        warnings.append("JSON 没有运动计数桶")

    diffs = np.diff(times_ms)
    positive_diffs = diffs[diffs > 0]
    if positive_diffs.size:
        median_step_ms = float(np.median(positive_diffs))
        gap_threshold_ms = max(100.0, median_step_ms * 4.0)
        gap_indices = np.flatnonzero(diffs > gap_threshold_ms).astype(np.int64) + 1
        max_gap_ms = float(np.max(diffs))
        sample_rate_hz = 1000.0 / median_step_ms
    else:
        median_step_ms = DEFAULT_FIXED_INTERVAL_MS
        gap_threshold_ms = max(100.0, median_step_ms * 4.0)
        gap_indices = np.empty(0, dtype=np.int64)
        max_gap_ms = 0.0
        sample_rate_hz = 1000.0 / median_step_ms
    _readonly(gap_indices)
    if gap_indices.size:
        warnings.append(
            f"检测到 {gap_indices.size} 处采样间隙，最大 {max_gap_ms:.0f} ms；"
            "绘图时应断线显示"
        )

    if (
        motion.size
        and not np.any(motion)
        and sum(float(np.std(channels[key])) for key in ("ax", "ay", "az")) < 0.4
    ):
        warnings.append("该段运动计数全为 0 且加速度近静止，可能缺少可见行为事件")

    create_time_ms = _normalise_epoch_ms(
        obj.get("create_time"), "create_time", required=True
    )
    assert create_time_ms is not None
    update_time_ms = _normalise_epoch_ms(
        obj.get("update_time"), "update_time", required=False
    )
    device = str(
        obj.get("device")
        or obj.get("device_id")
        or obj.get("mac")
        or obj.get("uid")
        or ""
    )
    metadata = {
        key: value
        for key, value in obj.items()
        if key not in {"imu", "temperature", "motion", "configs"}
    }

    return SessionData(
        source_path=Path(source_path),
        uid=obj.get("uid"),
        device=device,
        version=version,
        version_inferred=inferred,
        frame_bytes=_FRAME_BYTES[version],
        timestamp_mode=_TIMESTAMP_MODES[version],
        acc_scale=acc_scale,
        create_time_ms=create_time_ms,
        update_time_ms=update_time_ms,
        configs=_parse_configs(obj.get("configs"), warnings),
        times_ms=times_ms,
        channels=channels,
        temperature_times_ms=temperature_times,
        motion_times_ms=motion_times,
        duration_ms=duration_ms,
        sample_rate_hz=sample_rate_hz,
        gap_threshold_ms=gap_threshold_ms,
        gap_indices=gap_indices,
        max_gap_ms=max_gap_ms,
        warnings=tuple(warnings),
        metadata=metadata,
    )


def load_motion_json(
    path: str | Path,
    acc_scale: int = DEFAULT_ACCEL_SCALE,
) -> SessionData:
    """Read and decode one real recorder JSON file."""

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"找不到 JSON 文件：{source}")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidMotionDataError(f"JSON 不是 UTF-8 文本：{source}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidMotionDataError(
            f"JSON 语法错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}"
        ) from exc
    return parse_motion_object(obj, source_path=source.resolve(), acc_scale=acc_scale)


def load_json(
    path: str | Path,
    acc_scale: int = DEFAULT_ACCEL_SCALE,
) -> SessionData:
    """Compatibility alias for :func:`load_motion_json`."""

    return load_motion_json(path, acc_scale=acc_scale)


def _synthetic_object(version: int, *, declare_version: bool = True) -> dict[str, Any]:
    frames = bytearray()
    axes_rows = (
        (4096, 0, -4096, 32, -32, 64, 1000, -1000, 500),
        (2048, 1024, -2048, 64, -64, 96, 2000, -2000, 750),
        (0, 4096, 0, 96, -96, 128, 3000, -3000, 1000),
    )
    for index, axes in enumerate(axes_rows):
        if version == 1:
            frames.extend(struct.pack("<H", 0 if index == 0 else 20))
        elif version == 2:
            frames.extend(struct.pack("<I", 1000 + index * 20))
        frames.extend(struct.pack("<9h", *axes))
    obj: dict[str, Any] = {
        "uid": 1,
        "device": "SELFTEST",
        "create_time": 1_700_000_000_000,
        "update_time": 1_700_000_000_100,
        "configs": "{}",
        "imu": base64.b64encode(frames).decode("ascii"),
        "temperature": base64.b64encode(struct.pack("<2h", 3850, 3860)).decode("ascii"),
        "motion": base64.b64encode(struct.pack("<2H", 0, 3)).decode("ascii"),
    }
    if declare_version:
        obj["version"] = version
    return obj


def run_self_test() -> dict[str, Any]:
    """Exercise all frame layouts and missing-version inference."""

    tested: list[dict[str, Any]] = []
    for version in (0, 1, 2):
        declared = version == 1
        data = parse_motion_object(
            _synthetic_object(version, declare_version=declared),
            source_path=f"<synthetic-v{version}>",
        )
        assert data.version == version
        assert data.sample_count == 3
        assert np.allclose(data.times_ms, (0.0, 20.0, 40.0))
        assert np.isclose(data.channels["ax"][0], GRAVITY_MS2)
        assert np.isclose(data.temperature[0], 38.50)
        assert data.motion.tolist() == [0.0, 3.0]
        assert len(data.plot_series()) == 11
        tested.append(data.quality_report())

    broken = _synthetic_object(0, declare_version=False)
    broken["imu"] = base64.b64encode(
        base64.b64decode(broken["imu"]) + b"\xff"
    ).decode("ascii")
    try:
        parse_motion_object(broken)
    except InvalidMotionDataError:
        damaged_rejected = True
    else:
        damaged_rejected = False
    assert damaged_rejected
    return {
        "ok": True,
        "synthetic_layouts": tested,
        "damaged_payload_rejected": damaged_rejected,
    }


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析牛尾环真实九轴 JSON")
    parser.add_argument("files", nargs="*", help="要检查的 JSON 文件")
    parser.add_argument("--acc-scale", type=int, default=DEFAULT_ACCEL_SCALE)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    reports: list[dict[str, Any]] = []
    ok = True
    if args.self_test:
        reports.append({"self_test": run_self_test()})
    for name in args.files:
        try:
            reports.append(load_motion_json(name, args.acc_scale).quality_report())
        except Exception as exc:  # CLI diagnostic boundary
            ok = False
            reports.append({"source_path": name, "error": str(exc)})
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if ok else 1


__all__ = [
    "MotionData",
    "SessionData",
    "MotionDataError",
    "UnsupportedRecordError",
    "InvalidMotionDataError",
    "load_motion_json",
    "load_json",
    "parse_motion_object",
    "run_self_test",
]


if __name__ == "__main__":
    raise SystemExit(_main())
