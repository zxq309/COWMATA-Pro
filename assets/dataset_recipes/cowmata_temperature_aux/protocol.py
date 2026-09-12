# -*- coding: utf-8 -*-
"""Frozen V1 protocol decoder. No imputation or outcome information here."""
import base64, hashlib
import numpy as np
from dataclasses import dataclass
from typing import Optional

HOUR = 3600000.0


@dataclass(frozen=True)
class ProtocolConfig:
    temperature_scale: float = 0.01


class PacketError(ValueError):
    """Rejected input. The module state is unchanged on ingestion validation errors."""

    def __init__(self, code, message):
        super().__init__(code + ": " + message)
        self.code = code


@dataclass(frozen=True)
class Context:
    cow_id: str
    device_id: str
    binding_id: str
    binding_start_ms: int
    binding_end_ms: Optional[int] = None

    def __post_init__(self):
        if not all(
            isinstance(x, str) and x.strip()
            for x in [self.cow_id, self.device_id, self.binding_id]
        ):
            raise ValueError("cow_id/device_id/binding_id must be nonempty strings")
        if type(self.binding_start_ms) is not int or self.binding_start_ms <= 0:
            raise ValueError(
                "binding_start_ms must be a positive Unix millisecond integer"
            )
        if self.binding_end_ms is not None and (
            type(self.binding_end_ms) is not int
            or self.binding_end_ms <= self.binding_start_ms
        ):
            raise ValueError("binding_end_ms must follow binding_start_ms")


def _integer(obj, key):
    value = obj.get(key)
    if type(value) is not int or value < 0:
        raise PacketError("BAD_METADATA", key + " must be a nonnegative integer")
    return value


def _blob(obj, key):
    try:
        if not isinstance(obj.get(key), str):
            raise ValueError("not a Base64 string")
        return base64.b64decode(obj[key], validate=True)
    except Exception as exc:
        raise PacketError("BAD_BASE64", key + ": " + str(exc))


def decode_packet(obj, context, config, received_at_ms):
    """Validate before mutation; estimate sample midpoints like the annotation tool."""
    if not isinstance(obj, dict):
        raise PacketError("BAD_JSON", "object required")
    if str(obj.get("device", "")).upper() != context.device_id.upper():
        raise PacketError("WRONG_DEVICE", "device/binding mismatch")
    create = _integer(obj, "create_time")
    version = _integer(obj, "version")
    uid = obj.get("uid")
    if uid is None or isinstance(uid, (dict, list, bool)):
        raise PacketError("BAD_METADATA", "uid must identify a packet")
    if create < context.binding_start_ms:
        raise PacketError("BEFORE_BINDING", "packet starts before current binding")
    temp = _blob(obj, "temperature")
    imu = _blob(obj, "imu")
    if not temp or len(temp) % 2:
        raise PacketError(
            "BAD_TEMPERATURE_LENGTH", "nonempty signed int16 payload required"
        )
    width = {0: 18, 1: 20, 2: 22}.get(version)
    if width is None:
        raise PacketError("UNSUPPORTED_VERSION", str(version))
    if len(imu) % width or len(imu) // width < 2:
        raise PacketError("BAD_IMU_LENGTH", "complete IMU frames required")
    frames = len(imu) // width
    if version == 2:
        tick = np.ndarray((frames,), dtype="<u4", buffer=imu, strides=(22,)).astype(
            np.int64
        )
        dif = np.diff(tick)
        wrap = (tick[:-1] > 0xF0000000) & (tick[1:] < 0x0FFFFFFF)
        dif = np.where(wrap, dif + 2 ** 32, dif)
    elif version == 1:
        dif = np.ndarray((frames,), dtype="<u2", buffer=imu, strides=(20,))[1:].astype(
            np.int64
        )
    else:
        dif = np.full(frames - 1, 20, dtype=np.int64)
    if (dif <= 0).any():
        raise PacketError("BAD_IMU_CLOCK", "clock must increase within packet")
    duration = int(dif.sum())
    raw = np.frombuffer(temp, dtype="<i2").astype(np.int64)
    step = duration / len(raw)
    if not 55000 <= step <= 65000:
        raise PacketError(
            "UNSUPPORTED_CADENCE", "expected approximately one temperature per minute"
        )
    if duration > 2 * HOUR:
        raise PacketError("OVERSIZED_PACKET", "supports up to two hours per packet")
    times = create + (np.arange(len(raw)) + 0.5) * step
    if (
        context.binding_end_ms is not None
        and create + duration > context.binding_end_ms
    ):
        raise PacketError(
            "CROSSES_BINDING_END",
            "a packet cannot straddle or follow a cow/device binding end",
        )
    if times[-1] > received_at_ms:
        raise PacketError(
            "FUTURE_OBSERVATION", "estimated sample time exceeds receipt time"
        )
    digest = hashlib.sha256(
        (str(version) + "|" + str(create) + "|" + str(uid)).encode()
        + context.device_id.upper().encode()
        + temp
        + imu
    ).hexdigest()
    values = raw * config.temperature_scale
    return dict(
        uid=str(uid),
        digest=digest,
        create_ms=create,
        end_ms=float(times[-1]),
        duration_ms=duration,
        times=times,
        values=values,
        raw=raw,
        step_ms=step,
        imu_max_dt_ms=int(dif.max()),
        imu_gaps_over_100ms=int((dif > 100).sum()),
    )
