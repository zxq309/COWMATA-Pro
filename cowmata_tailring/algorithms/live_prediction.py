"""Manually started chronological prediction from a selected JSON folder."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from cowmata_tailring.edge_download.csv_targets import cow_identity
from cowmata_tailring.temperature import read_temperature_record
from cowmata_tailring.workspace.device_identity import (
    DEVICE,
    parse_device_folder,
    source_device_folder,
)
from cowmata_tailring.workspace.sensor_records import parse_sensor_object
from cowmata_tailring.workspace.storage import atomic_json

from .decision import build_fusion, predict_decision, read_decision
from .registry import list_suites, read_suite


def single_input(file):
    file = Path(file).resolve()
    if not file.is_file() or file.suffix.lower() != '.json':
        raise ValueError('请选择一个原始传感 JSON 文件。')
    if file.stat().st_size > 384 * 1024 * 1024:
        raise ValueError('单份记录过大')
    raw = file.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or 'create_time' not in data:
        raise ValueError('请选择原始 Motion、PPG 或 Temp JSON。')
    device = str(data.get('device') or '').strip().upper()
    if not DEVICE.fullmatch(device):
        raise ValueError('JSON 内设备编号不是完整 12 位十六进制编号。')
    cow, mark = cow_identity(data['cow_id']) if str(data.get('cow_id') or '').strip() else ('', '')
    folder = source_device_folder(file)
    owner = None
    if folder is not None:
        try:
            owner = parse_device_folder(folder.name)
        except ValueError:
            pass
    if owner is not None:
        if owner.device_id != device or (cow and (cow != owner.cow_id or (mark and mark != owner.field_mark))):
            raise ValueError('JSON 内牛号或设备与所在目录不一致，请核对原始记录。')
        if not cow:
            cow, mark = owner.cow_id, owner.field_mark
    modality = 'motion' if 'imu' in data else 'temp' if isinstance(data.get('data'), (int, float)) else 'ppg'
    record = dict(asset_id=hashlib.sha256(raw).hexdigest(), cow_id=cow, field_mark=mark,
                  device_id=device, modality=modality, raw=str(file), events=[], review_coverage=[],
                  identity_eligible=bool(cow))
    if modality != 'temp':
        sensor = parse_sensor_object(data, file, kind=modality)
        record.update(duration_ms=sensor.duration_ms, start_epoch_ms=sensor.epoch_at(0))
    return file, data, record


def required_suites(model, home):
    available = list_suites(home)
    selected, selections = [], {}
    for expected in model.get('behavior_models', []):
        found = next((s for s in available if s['version'] == expected['version'] and s['hash'] == expected['sha256']), None)
        if found is None:
            raise ValueError('缺少决策模型训练时使用的行为模型：' + expected['version'])
        suite = read_suite(found['root'])
        if not set(expected['codes']).issubset({m['code'] for m in suite['models']}):
            raise ValueError('行为模型事件与决策训练记录不一致。')
        selected.append(str(suite['root']))
        selections[str(suite['root'])] = expected['codes']
    return selected, selections



def scan_folder(folder, progress=lambda *_: None):
    from .inputs import EXCLUDED
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('请选择包含连续多日 JSON 的文件夹。')
    files = sorted(p for p in folder.rglob('*.json') if not any(part in EXCLUDED or part.startswith('.') for part in p.relative_to(folder).parts) and not p.stem.endswith('.标注'))
    records, temperatures, issues, owners, conflicts = {}, [], [], {}, set()
    for number, file in enumerate(files, 1):
        try:
            file, data, record = single_input(file)
            owner = (record['cow_id'], record['device_id'], record['field_mark'])
            asset = record['asset_id']
            if asset in owners and owners[asset] != owner:
                conflicts.add(asset)
                raise ValueError('相同记录对应不同牛号，请核对来源。')
            owners[asset] = owner
            if record['modality'] == 'temp':
                if not record['cow_id']:
                    raise ValueError('独立温度缺少牛号，不能跨文件推测关联。')
                sample = read_temperature_record(data)
                temperatures.append(dict(cow=record['cow_id'], device=record['device_id'], mark=record['field_mark'],
                    time=sample['time'], value=sample['value'], source=str(file), available_at_ms=sample['available_at_ms'],
                    time_basis=sample['time_basis'], sample_id=sample['sample_id'], source_kind=sample['source_kind']))
            else:
                records.setdefault(asset, record)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append(dict(path=str(file), reason=str(exc)))
        progress(number, len(files), '读取文件夹中的原始 JSON')
    records = sorted((r for asset, r in records.items() if asset not in conflicts), key=lambda r: (r['start_epoch_ms'], r['raw']))
    fingerprint = hashlib.sha256(json.dumps([(r['asset_id'], r['cow_id'], r['device_id'], r['field_mark']) for r in records]).encode()).hexdigest()
    return dict(records=records, issues=issues, fingerprint=fingerprint), temperatures, len(files)


def temporal_summary(rows):
    from collections import defaultdict
    grouped = defaultdict(list)
    for row in rows:
        key = (row['cow_id'], row['device_id'], row['field_mark']) if row['cow_id'] else ('', row['device_id'], row['asset_id'])
        grouped[key].append(row)
    summaries, alerts = [], []
    for key, group in grouped.items():
        group.sort(key=lambda r: (r['start_epoch_ms'], r['end_epoch_ms']))
        first = group[0]['start_epoch_ms']
        last = max(r['end_epoch_ms'] for r in group)
        intervals = []
        effective = 0.0
        for row in group:
            a, b = row['start_epoch_ms'], row['end_epoch_ms']
            if intervals and a <= intervals[-1][1]:
                extra = max(0, b-intervals[-1][1])
                intervals[-1][1] = max(intervals[-1][1], b)
            else:
                extra = b-a
                intervals.append([a,b])
            effective += extra * max(row.get('motion_coverage') or 0, row.get('ppg_coverage') or 0)
            row['history_span_hours'] = max(0, (a-first)/3600000) if row['cow_id'] else 0
            row['history_status'] = '参考历史不足24小时' if row['history_span_hours'] < 24 else '参考跨度已达24小时，完整性见数据覆盖'
            row['forecast_start_ms'] = row['decision_epoch_ms']
            row['forecast_end_ms'] = row['decision_epoch_ms'] + row['horizon_hours']*3600000
        span = (last-first)/3600000
        gaps = [(b[0]-a[1])/3600000 for a,b in zip(intervals,intervals[1:])]
        summaries.append(dict(cow_id=key[0],device_id=key[1],start_epoch_ms=first,end_epoch_ms=last,
            span_hours=round(span,3),effective_signal_hours=round(effective/3600000,3),
            largest_gap_hours=round(max(gaps,default=0),3),windows=len(group),
            recommendation='建议连续3至7天；不足24小时标记参考历史不足'))
        current = None
        for row in sorted(group,key=lambda r:r['decision_epoch_ms']):
            if row['warning_level'] != '关注并复核':
                current = None
                continue
            when = row['decision_epoch_ms']
            if current is None or when-current['last_warning_ms'] > 30*60000:
                current = dict(cow_id=row['cow_id'], device_id=row['device_id'], first_warning_ms=when,
                    last_warning_ms=when, forecast_end_ms=row['forecast_end_ms'], max_score=row['risk_score'],windows=1)
                alerts.append(current)
            else:
                current.update(last_warning_ms=when, forecast_end_ms=row['forecast_end_ms'],
                               max_score=max(current['max_score'],row['risk_score']),windows=current['windows']+1)
    return summaries, alerts


def predict_folder(folder, model_path, output, cache, model_home, *, progress=lambda *_: None):
    from .analysis import write_table
    started = time.monotonic()
    _, model = read_decision(model_path)
    suites, selections = required_suites(model, model_home)
    index, temperatures, count = scan_folder(folder, progress)
    if not index['records']:
        raise ValueError('文件夹内没有可用于预测的 Motion 或 PPG 记录；只有温度不足以给出产犊风险。')
    output = Path(output)
    evidence = build_fusion(folder, suites, output, cache, selections=selections, progress=progress,
                           input_index=index, input_temperatures=temperatures)
    if not evidence['rows']:
        raise ValueError('文件夹内没有可用于预测的信号窗口，请核对数据问题记录。')
    result = predict_decision(evidence, model_path, output)
    coverage, alerts = temporal_summary(result['rows'])
    result.update(coverage=coverage, alert_windows=alerts)
    result['input'] = dict(mode='folder',path=str(Path(folder).resolve()),json_files=count,
        sensor_records=len(index['records']),temperature_records=len(temperatures),
        elapsed_seconds=round(time.monotonic()-started,3),recommended_history_hours=72,
        reference_history_hours=24,horizon_hours=model['horizon_hours'],
        interpretation='按采样窗口回放，在服务器收包后预测；各窗口只使用当时已收到的历史基线。预警窗口不是已确认产犊时刻。')
    atomic_json(output/'综合决策结果.json',result)
    write_table(output/'综合决策结果.csv',result['rows'])
    write_table(output/'数据覆盖.csv',coverage)
    write_table(output/'连续预警时段.csv',alerts, ['cow_id','device_id','first_warning_ms','last_warning_ms','forecast_end_ms','max_score','windows'])
    return result
