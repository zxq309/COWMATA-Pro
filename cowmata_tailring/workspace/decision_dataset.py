"""Continuous temperature/activity replay; labels are separate evaluation truth."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .mother_dataset import _write_table


def export_decision(root, *, progress=lambda *_:None, cancelled=lambda:False):
    root = Path(root)
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/dataset_recipes'))
    from cowmata_activity_aux.engine import ActivityModule
    from cowmata_activity_aux.protocol import Context as ActivityContext
    from cowmata_temperature_aux.engine import TemperatureModule
    from cowmata_temperature_aux.protocol import Context, ProtocolConfig, decode_packet
    sources = [json.loads(line) for line in (root/'sources.jsonl').read_text(encoding='utf-8').splitlines()]
    events = [json.loads(line) for line in (root/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    output = root/'综合决策'
    output.mkdir(exist_ok=True)
    packets = []
    for source in sources:
        doc = json.loads((root/source['path']).read_text(encoding='utf-8-sig'))
        packets.append((doc.get('update_time') or 0,source))
    packets.sort(key=lambda item:(item[0],item[1]['asset_id']))
    engines,temperatures,review = {},[],[]
    streams = [(output/name).open('w',encoding='utf-8') for name in ('activity.jsonl','temperature.jsonl')]
    counts = [0,0]
    try:
        for i,(received,source) in enumerate(packets):
            if cancelled():
                raise InterruptedError('综合决策导出已暂停')
            cow,device = source.get('cow_id'),source.get('device_id')
            if not cow or not source.get('identity_eligible',False):
                review.append(dict(asset_id=source['asset_id'],reason='identity_review'))
                continue
            if type(received) is not int or received <= 0:
                review.append(dict(asset_id=source['asset_id'],reason='missing_receipt_proxy'))
                continue
            doc = json.loads((root/source['path']).read_text(encoding='utf-8-sig'))
            key = (cow,device,source.get('field_mark',''))
            if key not in engines:
                start = min(int(s.get('record_start_ms') or received)-1000 for _,s in packets
                            if (s.get('cow_id'),s.get('device_id'),s.get('field_mark',''))==key)
                binding = '-'.join(str(k) for k in key)
                context = Context(cow,device,binding,start)
                engines[key] = (ActivityModule(ActivityContext(cow,device,binding,start)),TemperatureModule(context),context)
            activity,temperature,context = engines[key]
            for j,engine in enumerate((activity,temperature)):
                try:
                    result = engine.process_packet(doc,received_at_ms=received,evaluated_at_ms=received,
                                                   receive_time_source='json_update_proxy')
                    result.update(source_asset_id=source['asset_id'],truth_used_as_input=False)
                    streams[j].write(json.dumps(result,ensure_ascii=False)+'\n')
                    counts[j] += 1
                    if j == 1:
                        decoded = decode_packet(doc,context,ProtocolConfig(),received)
                        for stamp,raw,value in zip(decoded['times'],decoded['raw'],decoded['values']):
                            temperatures.append(dict(source_asset_id=source['asset_id'],cow_id=cow,device_id=device,
                                sample_epoch_ms=float(stamp),temperature_raw=int(raw),temperature_c=float(value),
                                time_basis='legacy_packet_midpoint_estimate',is_observed=True))
                except (ValueError,TypeError,KeyError) as exc:
                    review.append(dict(asset_id=source['asset_id'],module=('activity','temperature')[j],reason=str(exc)))
            progress(i+1,len(packets),'综合决策')
    finally:
        for stream in streams:
            stream.close()
    _write_table(output/'temperature_samples.csv',temperatures,
        ['source_asset_id','cow_id','device_id','sample_epoch_ms','temperature_raw','temperature_c','time_basis','is_observed'])
    _write_table(output/'review.csv',review,['asset_id','module','reason'])
    calving = [e for e in events if e['code']=='CALF_FULLY_EXPELLED']
    _write_table(output/'calving_T0.csv',calving,
        ['event_id','cow_id','start_epoch_ms','training_eligible','exclusion_reason','split','source_asset_id'])
    result = dict(activity_packets=counts[0],temperature_packets=counts[1],temperature_samples=len(temperatures),
                  review_records=len(review),calving_events=len(calving),truth_used_as_input=False,
                  receipt_time_basis='json_update_proxy',phase_policy='default_prepartum_no_truth_transition',
                  probability_calibrated=False)
    (output/'readiness.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
