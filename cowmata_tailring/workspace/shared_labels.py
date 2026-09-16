"""One behavioral truth, projected by verified identity and acquisition time.

Quality flags remain modality-specific. Missing data is never a negative label.
The original annotation owns edits; generated projections retain its identity.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

CONTRACT=dict(schema='cowmata-shared-labels-1',modalities=['Motion','PPG','Temp'],
    event_owner='source_annotation',join=['cow_id','device_id','field_mark','acquisition_epoch_ms'],
    timestamp_unit='unix_ms',resampling='none',missing_is_negative=False,
    sensor_quality_shared=False,temperature_estimated_time='preserve_provenance')

def project_events(events,target,origin,duration):
    result=[]
    for event in events:
        if event['source_asset_id']==target['asset_id']:continue
        if any(str(event.get(k,''))!=str(target.get(k,'')) for k in ('cow_id','device_id','field_mark')):continue
        if not event.get('cow_id') or not event.get('device_id'):continue
        start=event['start_epoch_ms']-origin
        ending=event.get('end_epoch_ms');end=ending-origin if ending is not None else None
        if start>duration or (end if end is not None else start)<0:continue
        truncated=start<0 or end is not None and end>duration
        result.append(dict(code=event['code'],start_ms=max(0,start),end_ms=min(duration,end) if end is not None else None,
            event_id=event['id'],shared_event_id=event['id'],source_asset_id=event['source_asset_id'],
            confirmation='needs_review' if truncated else event['confirmation'],boundary_truncated=truncated,
            label_source=event.get('label_source',''),projection='acquisition_epoch_ms'))
    return result

def link_records(records,issues,cancelled=lambda:False,*,collect_all=False):
    """Derive from the current label files on each scan, so edits cannot go stale."""
    from .sensor_records import parse_sensor_object
    from cowmata_tailring.temperature import validate_temperature_identity
    relevant=[r for r in records if r.get('identity_eligible',True) and not r.get('conflicts')]
    donors=[r for r in relevant if r['events']]
    candidates=[r for r in relevant if any(d['modality']!=r['modality'] and
        all(str(r.get(k,''))==str(d.get(k,'')) for k in ('cow_id','device_id','field_mark')) for d in donors)]
    if not candidates and not collect_all:return []
    needed={r['asset_id']:r for r in donors+candidates}
    clocks={};masters=[]
    for asset,row in needed.items():
        if cancelled():raise InterruptedError('共享标签读取已取消')
        try:
            raw=Path(row['raw'])
            content=raw.read_bytes()
            if hashlib.sha256(content).hexdigest()!=asset:raise ValueError('共享标签来源内容身份不一致')
            obj=json.loads(content.decode('utf-8-sig'))
            validate_temperature_identity(obj,row['cow_id'],row.get('field_mark',''))
            if str(obj.get('device','')).upper()!=str(row['device_id']).upper():raise ValueError('来源设备编号冲突')
            signal=parse_sensor_object(obj,raw,kind='ppg' if row['modality']=='ppg' else 'imu')
            clocks[asset]=(signal.epoch_at(0),signal.duration_ms)
            for event in row['events']:
                if event.get('projection'):continue
                start=event['start_ms'];end=event.get('end_ms')
                if start<0 or start>signal.duration_ms or end is not None and end>signal.duration_ms:
                    issues.append(dict(path=row['raw'],reason='共享标签超出原始采样范围'));continue
                identity=[asset,event.get('event_id')] if event.get('event_id') is not None else [asset,event['code'],start,end]
                identifier=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
                masters.append(dict(id=identifier,source_asset_id=asset,cow_id=row['cow_id'],device_id=row['device_id'],
                    field_mark=row.get('field_mark',''),code=event['code'],start_epoch_ms=signal.epoch_at(start),
                    end_epoch_ms=signal.epoch_at(end) if end is not None else None,
                    confirmation=event.get('confirmation',''),label_source=event.get('label_source','')))
        except (OSError,ValueError,TypeError,KeyError) as exc:
            issues.append(dict(path=row['raw'],reason='共享标签未关联：'+str(exc)))
    versions={}
    for event in masters:
        versions.setdefault(event['id'],[]).append(event)
    for identifier,variants in versions.items():
        if len({(e['code'],e['start_epoch_ms'],e.get('end_epoch_ms')) for e in variants})>1:
            issues.append(dict(reason='同一源标签存在冲突修订，共享结果待复核',shared_event_id=identifier))
            for event in variants:event['confirmation']='needs_review'
    for row in candidates:
        if row['asset_id'] not in clocks:continue
        origin,duration=clocks[row['asset_id']]
        projected=project_events(masters,row,origin,duration)
        existing={(e['code'],e['start_ms'],e.get('end_ms')) for e in row['events']}
        for event in projected:
            key=(event['code'],event['start_ms'],event.get('end_ms'))
            if key in existing:continue
            # Preserve independently reviewed boundaries; surface disagreements.
            conflicts=[e for e in row['events'] if not e.get('projection') and e['code']==event['code'] and
                e['start_ms']<=(event.get('end_ms') or event['start_ms']) and
                (e.get('end_ms') or e['start_ms'])>=event['start_ms']]
            if conflicts:
                issues.append(dict(path=row['raw'],reason='同一行为在不同传感器标签中的边界冲突，请复核',shared_event_id=event['shared_event_id']))
                for old in conflicts:old['confirmation']='needs_review'
                continue
            row['events'].append(event);existing.add(key)
        row['events'].sort(key=lambda e:(e['start_ms'],e['code']))
    return masters


def validate_contract(value):
    if value is not None and value!=CONTRACT:
        raise ValueError('模型的共享行为标签协议不兼容')
