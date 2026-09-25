"""Read COWMATA motion JSON (base64 IMU frames) without the GUI package.

The decoder mirrors ``cowmata_tailring.annotation.data``: v0 = 9 x int16 at a
fixed 20 ms, v1 = uint16 delta + 9 x int16, v2 = uint32 device ms + 9 x int16.
Times are relative to the first frame, i.e. the ``parent_imu_ms`` coordinates
used by the annotation files.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ACC_LSB_PER_G = 4096.0
GYRO_LSB_PER_DPS = 32.0
MAG_LSB = 1000.0
_FRAME = {0: 18, 1: 20, 2: 22}


@dataclass
class Recording:
    path: Path
    device: str
    create_time_ms: int
    version: int
    times_ms: np.ndarray  # float64, starts at 0
    acc_g: np.ndarray  # (n, 3)
    gyro_dps: np.ndarray  # (n, 3)
    mag: np.ndarray  # (n, 3)
    raw_axes: np.ndarray  # (n, 9) int16 counts, for saturation checks

    @property
    def duration_s(self) -> float:
        return float(self.times_ms[-1]) / 1000.0 if len(self.times_ms) else 0.0


def _timestamp_score(raw: bytes) -> float:
    r = np.frombuffer(raw[: len(raw) // 22 * 22], dtype=[("t", "<u4"), ("a", "<i2", (9,))])
    if len(r) < 3:
        return 0.0
    d = np.diff(r["t"].astype(np.int64))
    return float(np.mean((d > 0) & (d < 1000)))


def _choose_version(raw: bytes, declared) -> int:
    if declared not in (None, ""):
        v = int(declared)
        if v not in _FRAME or len(raw) % _FRAME[v]:
            raise ValueError(f"imu 字节数 {len(raw)} 与声明的 v{v} 帧长不符")
        return v
    if len(raw) % 22 == 0 and _timestamp_score(raw) >= 0.9:
        return 2
    if len(raw) % 18 == 0:
        return 0
    if len(raw) % 20 == 0:
        return 1
    raise ValueError("无法可靠推断 IMU 帧格式")


def decode_object(obj: dict, path: str | Path = "<memory>") -> Recording:
    encoded = obj.get("imu")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("缺少 imu Base64 字段")
    raw = base64.b64decode("".join(encoded.split()), validate=True)
    version = _choose_version(raw, obj.get("version"))
    if version == 2:
        r = np.frombuffer(raw, dtype=[("t", "<u4"), ("a", "<i2", (9,))])
        t = r["t"].astype(np.int64)
        d = np.diff(t)
        wrap = (t[:-1] > 0xF0000000) & (t[1:] < 0x0FFFFFFF)
        if np.any((d < 0) & ~wrap):
            raise ValueError("v2 设备时间戳倒退")
        d = np.where(wrap, d + 2 ** 32, d)
        times = np.r_[0.0, np.cumsum(d, dtype=np.float64)]
        axes = r["a"]
    elif version == 1:
        r = np.frombuffer(raw, dtype=[("d", "<u2"), ("a", "<i2", (9,))])
        times = np.r_[0.0, np.cumsum(r["d"][1:].astype(np.float64))]
        axes = r["a"]
    else:
        axes = np.frombuffer(raw, dtype="<i2").reshape(-1, 9)
        times = np.arange(len(axes), dtype=np.float64) * 20.0
    if len(axes) < 2:
        raise ValueError("IMU 帧不足 2 条")
    keep = np.r_[True, np.diff(times) > 0]
    times, axes = times[keep], np.asarray(axes[keep])
    f = axes.astype(np.float32)
    return Recording(
        path=Path(path), device=str(obj.get("device") or ""), create_time_ms=int(obj.get("create_time") or 0),
        version=version, times_ms=times, acc_g=f[:, 0:3] / ACC_LSB_PER_G, gyro_dps=f[:, 3:6] / GYRO_LSB_PER_DPS,
        mag=f[:, 6:9] / MAG_LSB, raw_axes=axes)


def load_recording(path: str | Path) -> Recording:
    path = Path(path)
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    return decode_object(obj, path)
