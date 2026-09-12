"""Strict, read-only decoder for the calibrated-form V2 <I9h> IMU protocol."""
from __future__ import annotations
import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
import numpy as np


class PacketError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f'{code}: {message}')


def timestamp(value, name='timestamp'):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 0:
        raise PacketError('INVALID_TIME', f'{name} must be a nonnegative Unix millisecond integer')
    return int(value)


def device_key(value):
    return str(value).replace(':', '').replace('-', '').strip().upper()


@dataclass(frozen=True)
class Context:
    cow_id: str
    device_id: str
    binding_id: str
    binding_start_ms: int

    def __post_init__(self):
        for name in ['cow_id', 'device_id', 'binding_id']:
            original = getattr(self, name)
            if original is None or isinstance(original, bool):
                raise ValueError(f'{name} must be an explicit identifier')
            value = str(original).strip()
            if not value:
                raise ValueError(f'{name} must not be empty')
            object.__setattr__(self, name, value)
        object.__setattr__(self, 'device_id', device_key(self.device_id))
        if not self.device_id:
            raise ValueError('device_id must contain an identifier')
        object.__setattr__(self, 'binding_start_ms', timestamp(self.binding_start_ms, 'binding_start_ms'))


@dataclass
class DecodedPacket:
    uid: str
    fingerprint: str
    create_ms: int
    elapsed_ms: np.ndarray
    acceleration_counts: np.ndarray
    payload_bytes: int

    @property
    def start_ms(self):
        return self.create_ms + int(self.elapsed_ms[0])

    @property
    def end_ms(self):
        return self.create_ms + int(self.elapsed_ms[-1])


FRAME_DTYPE = np.dtype([('elapsed_ms', '<u4'), ('values', '<i2', (9,))], align=False)


def decode_packet(document: Mapping[str, Any], context: Context, *, max_duration_ms=7200000):
    if not isinstance(document, Mapping):
        raise PacketError('INVALID_DOCUMENT', 'packet must be a mapping')
    if device_key(document.get('device', '')) != context.device_id:
        raise PacketError('DEVICE_MISMATCH', 'packet does not belong to this binding')
    if type(document.get('version')) is not int or document.get('version') != 2:
        raise PacketError('UNSUPPORTED_VERSION', 'only V2 22-byte IMU frames are supported')
    uid = document.get('uid')
    if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not str(uid).strip():
        raise PacketError('INVALID_UID', 'uid must be a nonempty string or integer')
    create_ms = timestamp(document.get('create_time'), 'create_time')
    encoded = document.get('imu')
    if not isinstance(encoded, str) or not encoded:
        raise PacketError('MISSING_IMU', 'imu must be a nonempty Base64 string')
    # Bound allocation; 2x nominal cadence allowance before decoding.
    if len(encoded) > int(max_duration_ms / 10 * 22 * 4 / 3 + 1024):
        raise PacketError('PACKET_TOO_LARGE', 'encoded IMU exceeds the supported packet duration')
    try:
        payload = base64.b64decode(''.join(encoded.split()), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise PacketError('INVALID_BASE64', 'imu Base64 is invalid') from exc
    if len(payload) < 220 or len(payload) % 22:
        raise PacketError('INVALID_FRAME_LAYOUT', 'expected at least 10 complete <I9h> frames; no bytes are trimmed')
    frames = np.frombuffer(payload, dtype=FRAME_DTYPE)
    elapsed = frames['elapsed_ms'].astype(np.int64)
    dt = np.diff(elapsed)
    if np.any(dt <= 0):
        raise PacketError('NONMONOTONIC_SAMPLES', 'IMU timestamps must strictly increase')
    if elapsed[-1] - elapsed[0] > max_duration_ms or elapsed[-1] > max_duration_ms + 60000:
        raise PacketError('PACKET_TOO_LONG', 'packet exceeds the supported recording duration')
    if not 10 <= np.median(dt) <= 40:
        raise PacketError('UNEXPECTED_CADENCE', 'V2 frame cadence is incompatible with the 50 Hz profile')
    if create_ms + int(elapsed[0]) < context.binding_start_ms:
        raise PacketError('BEFORE_BINDING', 'packet contains samples before this binding began')
    digest = hashlib.sha256()
    digest.update(f'{context.device_id}|{create_ms}|2|'.encode('utf-8'))
    digest.update(payload)
    return DecodedPacket(str(uid), digest.hexdigest(), create_ms, elapsed,
                         frames['values'][:, :3].copy(), len(payload))
