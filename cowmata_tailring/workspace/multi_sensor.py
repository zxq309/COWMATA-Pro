"""Load related signals on their acquisition clock; never resample or copy labels."""
from __future__ import annotations
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import numpy as np
from PySide6.QtCore import QObject, Signal
from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.temperature import CHINA, read_temperature_record, validate_temperature_identity
from .catalog import file_stamp
from .device_identity import parse_device_folder, resolve_device_identity
from .resource_layout import day_at
from .sensor_records import load_sensor_json

KINDS = ('motion','ppg','temp')

def split_series(series):
    ppg=any(s.key.startswith('ppg_') for s in series)
    return dict(motion=[] if ppg else [s for s in series if s.key!='temperature'],
                ppg=[s for s in series if s.key!='temperature'] if ppg else [],
                temp=[s for s in series if s.key=='temperature'])

def identity_for(path, device):
    path=Path(path)
    if path.parent.name=='Raw' and path.stem.endswith('_raw'):
        value=parse_device_folder(path.stem.split('_',1)[0])
        if value.device_id!=str(device).upper():raise ValueError('设备编号冲突')
        return dict(status='ready',cow_id=value.cow_id,device_id=value.device_id,field_mark=value.field_mark)
    return resolve_device_identity(path,device)

def scope_for(path, root):
    for parent in Path(path).parents:
        if parent.name.casefold() in {'motion','ppg','temp'}:
            return parent.parent
    return Path(root)

def related_files(scope, kind, start, end, identity, cancelled):
    from datetime import datetime
    # Restrict directory work to the current record's days and previous day for
    # cross-midnight packets. Never recursively scan the entire farm on a tab click.
    first=datetime.fromtimestamp(start/1000,CHINA).date()-timedelta(days=1)
    last=datetime.fromtimestamp(end/1000,CHINA).date()
    if (last-first).days>32:raise ValueError('单次曲线浏览范围超过 31 天，请选择较短记录')
    modality=next((p for p in scope.iterdir() if p.is_dir() and p.name.casefold()==kind.casefold()),None)
    if modality is None:return
    day=first
    while day<=last:
        if cancelled():raise InterruptedError()
        folder=modality/day.isoformat();day+=timedelta(days=1)
        if not folder.is_dir():continue
        for owner in folder.iterdir():
            if not owner.is_dir() or owner.is_symlink() or getattr(owner,'is_junction',lambda:False)():continue
            try:known=parse_device_folder(owner.name)
            except ValueError:continue
            if (known.cow_id,known.device_id,known.field_mark)!=(identity['cow_id'],identity['device_id'],identity['field_mark']):continue
            for file in owner.iterdir():
                if cancelled():raise InterruptedError()
                if file.is_file() and file.suffix.lower()=='.json' and not file.is_symlink() and not file.name.endswith('.标注.json'):
                    yield file

def load_related(primary, root, cow_id, cancelled=lambda:False):
    base=split_series([PlotSeries(**s) for s in primary.plot_series()])
    result=dict(modalities=base,messages={},issues=[],sources=[])
    identity=identity_for(primary.source_path,primary.device)
    if not cow_id or identity.get('status')!='ready' or identity.get('cow_id')!=str(cow_id):
        result['messages']={k:'牛号或设备绑定待核对，未自动关联其他记录' for k in KINDS}
        return result
    source_obj=json.loads(Path(primary.source_path).read_text(encoding='utf-8-sig'))
    try:
        validate_temperature_identity(source_obj,cow_id,identity['field_mark'])
    except ValueError:
        result['messages']={k:'来源 JSON 牛号或现场记号与当前对象冲突，未关联其他记录' for k in KINDS}
        return result
    origin=float(primary.epoch_at(0));ending=float(primary.epoch_at(primary.duration_ms))
    scope=scope_for(primary.source_path,root)
    own='ppg' if getattr(primary,'kind','imu')=='ppg' else 'motion'
    values={};temperature={};estimated=False
    for kind,directory in [('motion','Motion'),('ppg','PPG'),('temp','Temp')]:
        if kind==own:continue
        for file in related_files(scope,directory,origin,ending,identity,cancelled):
            if cancelled():raise InterruptedError()
            try:
                before=file_stamp(file)
                data=json.loads(file.read_text(encoding='utf-8-sig'))
                validate_temperature_identity(data,cow_id,identity['field_mark'])
                if str(data.get('device','')).upper()!=identity['device_id']:
                    raise ValueError('JSON 设备与当前对象冲突')
                if kind=='temp':
                    sample=read_temperature_record(data)
                    when=sample['time']
                    if not origin<=when<=ending:continue
                    if file_stamp(file)!=before:raise ValueError('读取期间文件变化')
                    key=sample.get('sample_id') or (str(data.get('device')),when,sample['value'])
                    pair=(when-origin,sample['value'])
                    if key in temperature and temperature[key]!=pair:
                        raise ValueError('同一温度采样身份存在不同数值')
                    temperature[key]=pair
                    estimated|=sample['time_basis']=='imu_bucket_midpoint_estimate'
                else:
                    from .sensor_records import parse_sensor_object
                    signal=parse_sensor_object(data,file,kind='ppg' if kind=='ppg' else 'imu')
                    if signal.epoch_at(signal.duration_ms)<origin or signal.epoch_at(0)>ending:continue
                    if file_stamp(file)!=before:raise ValueError('读取期间文件变化')
                    for item in signal.plot_series():
                        if item['key']=='temperature':continue
                        times=np.asarray(item['times_ms'])+signal.epoch_at(0)-origin
                        mask=(times>=0)&(times<=primary.duration_ms)
                        if not mask.any():continue
                        values.setdefault((kind,item['key']),[]).append((item,times[mask],np.asarray(item['values'])[mask]))
                result['sources'].append(dict(path=str(file),stamp=before,kind=kind))
            except (OSError,ValueError,TypeError,KeyError) as exc:
                result['issues'].append(dict(path=str(file),message=str(exc)))
    for (kind,key),pieces in values.items():
        times=np.concatenate([p[1] for p in pieces]);samples=np.concatenate([p[2] for p in pieces])
        order=np.argsort(times,kind='stable');times,samples=times[order],samples[order]
        # Preserve overlap disagreements as a visible gap, never choose a value.
        unique,starts,counts=np.unique(times,return_index=True,return_counts=True)
        output=np.array([samples[i] if n==1 or np.all(samples[i:i+n]==samples[i]) else np.nan for i,n in zip(starts,counts)])
        if np.isnan(output).any():result['issues'].append(dict(message='重叠信号值冲突；以缺口显示'))
        item=pieces[0][0]
        base[kind].append(PlotSeries(key,item['name'],item['unit'],item['color'],unique,output))
    if temperature:
        samples=sorted(set(temperature.values()))
        # Conflicting scalar values at the same timestamp are not averaged.
        grouped={}
        for t,value in samples:grouped.setdefault(t,set()).add(value)
        times=sorted(grouped)
        base['temp']=[PlotSeries('temperature','温度 °C','°C','#d59338',np.array(times),
            np.array([next(iter(grouped[t])) if len(grouped[t])==1 else np.nan for t in times]))]
        result['messages']['temp']='独立温度 JSON · 摄氏度' + ('；时间为区间中点估计' if estimated else '')
    elif base['temp']:
        result['messages']['temp']='来源内附温度；时间为区间中点估计'
    for key in KINDS:
        result['messages'].setdefault(key,'按本次设备采集时钟对齐，保留原始采样与缺口')
    if result['issues']:
        for key in KINDS:result['messages'][key]+=f'；{len(result["issues"])} 项来源问题未强行合并'
    return result


class RelatedSignalLoader(QObject):
    ready=Signal(object)
    def __init__(self,panel):
        super().__init__(panel)
        self.panel=panel;self.generation=0;self.cancellation=threading.Event()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='related-signals')
        self.future=None
        self.ready.connect(self.apply)
        self.destroyed.connect(lambda *_: self.close())
    def cancel(self):
        self.generation+=1;self.cancellation.set()
        if self.future:self.future.cancel()
    def close(self):
        self.cancel();self.pool.shutdown(wait=False,cancel_futures=True)
    def load(self,primary,root,cow_id):
        self.cancel();self.cancellation=threading.Event()
        generation=self.generation;cancel=self.cancellation
        def read():
            try:result=load_related(primary,root,cow_id,cancel.is_set)
            except InterruptedError:return
            except Exception as exc:
                result=dict(modalities=split_series([PlotSeries(**s) for s in primary.plot_series()]),
                    messages={k:'关联数据读取失败：'+str(exc) for k in KINDS},issues=[str(exc)])
            if not cancel.is_set():
                try:self.ready.emit((generation,result))
                except RuntimeError:pass
        self.future=self.pool.submit(read)
    def apply(self,value):
        generation,result=value
        if generation!=self.generation:return
        self.panel.related_result=result
        self.panel.update_modalities(result['modalities'],result['messages'])
