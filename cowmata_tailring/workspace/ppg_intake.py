"""Archive original PPG JSON and retain verified configured waveform timing."""
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
    elif not isinstance(payload, list | dict):
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
    metadata = {'modality':'PPG','archive_only':True,'duration_unknown':True}
    hi = lo+1
    try:
        from .sensor_records import parse_ppg_object
        signal = parse_ppg_object(obj,path)
        hi = signal.epoch_at(signal.duration_ms)
        metadata.update(archive_only=False,duration_unknown=False,duration_ms=signal.duration_ms,
                        sample_rate_hz=signal.sample_rate_hz,samples=signal.sample_count,
                        capture_timing=signal.capture_timing())
    except (ValueError,TypeError,KeyError) as exc:
        metadata['parser_issue'] = str(exc)
    day = day_at(lo)
    target = root / 'PPG' / day / owner / (start_stamp(lo) + '.json')
    if target.exists() and digest(target, cache, cancelled) != sha:
        raise ValueError('目标同名但内容不同，停止覆盖')
    row.update(status='existing' if target.exists() else 'ready', target=str(target),
               sha256=sha, owner=owner, batch=day, record_date=day, covered_dates=[day],
               record_start_ms=lo, record_end_ms=hi, timezone_offset_minutes=480,
               time_basis='unix_epoch_ms', transfer='move' if transfer == 'move' and core.volume(path) == core.volume(root) else 'copy',
               metadata=metadata,
               message='按 PPG JSON 采集日期归类；原始数据保持不变')
    return row


def plan_temperature(path, root, cache, transfer, cancelled, *, digest):
    """Archive existing Temp records byte-for-byte using the shared contract."""
    from cowmata_tailring.temperature import CONTRACT, read_temperature_record, temperature_owner

    path = Path(path)
    kinds = [part.casefold() for part in reversed(path.parent.parts)
             if part.casefold() in {'temp', 'motion', 'ppg'}]
    if not kinds or kinds[0] != 'temp':
        return None
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    # Windows stores ordinary downloads under AppData/Local/Temp. Explicit
    # waveform fields take precedence over that ancestor directory name.
    if isinstance(obj, dict) and any(key in obj for key in ('imu', 'ppg')):
        return None
    sample = read_temperature_record(obj)
    owner = temperature_owner(path, obj)
    day = day_at(sample['time'])
    target = root / 'Temp' / day / owner / (start_stamp(sample['time']) + '.json')
    sha = digest(path, cache, cancelled)
    if target.exists() and digest(target, cache, cancelled) != sha:
        raise ValueError('目标同名但内容不同，停止覆盖')
    naming = core.resolve_device_identity(path, obj['device'])
    return dict(source=str(path), kind='temp', size=path.stat().st_size,
                identity=core.identity(path), device=obj['device'],
                target=str(target), sha256=sha, owner=owner, batch=day, record_date=day,
                record_start_ms=sample['time'], record_end_ms=sample['time']+1,
                covered_dates=[day], timezone_offset_minutes=480, time_basis='unix_epoch_ms',
                status='existing' if target.exists() else 'ready',
                transfer='move' if transfer == 'move' and core.volume(path) == core.volume(root) else 'copy',
                cow_id=naming.get('cow_id',''), device_id=obj['device'], field_mark=naming.get('field_mark',''),
                metadata={'modality':'Temp','temperature_contract':dict(CONTRACT)},
                message='按温度采集时间归类；保留原始摄氏温度记录')
