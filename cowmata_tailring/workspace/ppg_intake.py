"""Archive original PPG JSON by acquisition time, without claiming waveform support."""
import base64
import json
from pathlib import Path

from . import organization as core
from .resource_layout import day_at, start_stamp


def plan_ppg(path, root, cache, transfer, cancelled, *, digest):
    from cowmata_tailring.annotation.data import _normalise_epoch_ms

    from .device_identity import resolve_device_identity
    path = Path(path)
    if path.stat().st_size > 64 * 1024**2:
        raise ValueError('PPG JSON 超过 64 MiB，请检查原始记录')
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict) or obj.get('imu'):
        return None
    in_ppg = any(p.lower() == 'ppg' for p in path.parts)
    payload = obj.get('ppg') if 'ppg' in obj else obj.get('data') if in_ppg else None
    if payload is None:
        return None
    if not payload:
        raise ValueError('PPG 数据为空')
    if isinstance(payload, str):
        if not base64.b64decode(payload, validate=True):
            raise ValueError('PPG 数据为空')
    elif not isinstance(payload, (list, dict)):
        raise ValueError('PPG 数据格式无法识别')
    lo = _normalise_epoch_ms(obj.get('create_time'), 'create_time', required=True)
    device = str(obj.get('device') or obj.get('device_id') or '').strip()
    if not device:
        raise ValueError('PPG JSON 缺少设备号')
    row = dict(source=str(path), kind='ppg', size=path.stat().st_size,
               identity=core.identity(path), device=device)
    naming = resolve_device_identity(path, device)
    owner = naming.get('folder_name') if naming.get('status') == 'ready' else core.safe_name(device)
    row.update({k: v for k, v in naming.items() if k not in {'status', 'message'}})
    sha = digest(path, cache, cancelled)
    day = day_at(lo)
    target = root / 'PPG' / day / owner / (start_stamp(lo, milliseconds=True) + '.json')
    if target.exists() and digest(target, cache, cancelled) != sha:
        target = target.with_name(target.stem + '__' + sha[:12] + '.json')
    row.update(status='existing' if target.exists() else 'ready', target=str(target),
               sha256=sha, owner=owner, batch=day, record_date=day, covered_dates=[day],
               record_start_ms=lo, record_end_ms=lo+1, timezone_offset_minutes=480,
               time_basis='unix_epoch_ms', transfer='move' if transfer == 'move' and core.volume(path) == core.volume(root) else 'copy',
               metadata={'modality': 'PPG', 'archive_only': True, 'duration_unknown': True},
               message='按 PPG JSON 采集日期归类；原始数据保持不变')
    return row
