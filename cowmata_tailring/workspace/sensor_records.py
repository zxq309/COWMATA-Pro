"""Read Motion and configured PPG without altering or resampling source bytes."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class PPGData:
    source_path: Path
    create_time_ms: int
    sample_rate_hz: float
    times_ms: np.ndarray
    channels: dict
    device: str = ''
    kind: str = 'ppg'
    version: int = 0
    acc_scale: int = 4096
    coordinate_offset_ms: float = 0.0
    first_frame_elapsed_ms: float = 0.0
    update_time_ms: int | None = None
    warnings: list = field(default_factory=list)
    frame_bytes: int = 0

    @property
    def uid(self):
        return self.create_time_ms

    def quality_report(self):
        return dict(kind='ppg', sample_count=self.sample_count, duration_ms=self.duration_ms,
                    sample_rate_hz=self.sample_rate_hz, warnings=self.warnings, gap_count=0)

    @property
    def duration_ms(self):
        return float(self.times_ms[-1]) if len(self.times_ms) else 0.0

    @property
    def sample_count(self):
        return len(self.times_ms)

    def epoch_at(self, source_ms):
        if self.create_time_ms <= 0:
            raise ValueError('PPG 缺少有效采集时间')
        return self.create_time_ms + source_ms

    def capture_timing(self):
        return dict(basis='ppg_configured_rate', sample_rate_hz=self.sample_rate_hz,
                    sample_start_epoch_ms=self.epoch_at(0), sample_end_epoch_ms=self.epoch_at(self.duration_ms),
                    camera_alignment='not_implied')

    def plot_series(self):
        colors=['#159c8d','#627de5','#d59338']
        return [dict(key=key,name=name,unit=unit,color=colors[i%3],times_ms=self.times_ms,values=values)
                for i,(key,(name,unit,values)) in enumerate(self.channels.items())]

    def nearest_sample_index(self, value):
        return max(0,min(self.sample_count-1,int(round(value*self.sample_rate_hz/1000))))


def parse_ppg_object(data, source_path):
    config=data.get('configs') or {}
    if isinstance(config,str):
        config=json.loads(config)
    if not isinstance(config,dict):
        raise ValueError('PPG 采样配置格式错误')
    rates=(50,100,200,400,800,1000,1600,3200)
    sr,average=config.get('pulse_led_sr'),config.get('pulse_led_avr')
    frequency=float(data.get('sample_rate_hz') or 0)
    if sr is not None and average is not None:
        sr,average=int(sr),int(average)
        if 0<=sr<len(rates) and 0<=average<=10:
            frequency=rates[sr]/(1<<average)
    duration=float(config.get('pulse_sample_time') or 0)
    def decode(name):
        value=data.get(name)
        if not value:
            return b''
        return base64.b64decode(''.join(value.split()),validate=True)
    acc=decode('imu_data')
    if len(acc)%6:
        raise ValueError('PPG 同步加速度长度无效')
    acc_count=len(acc)//6
    channels={}
    counts=[]
    for channel_field,key,name in [('data','ppg_primary','PPG 主通道'),('ir_data','ppg_ir','PPG 红外'),('red_data','ppg_red','PPG 红光')]:
        raw=decode(channel_field)
        if not raw:
            continue
        width=0
        if acc_count:
            width=4 if len(raw)==acc_count*4 else 2 if len(raw)==acc_count*2 else 0
        elif frequency>0 and duration>0:
            expected=round(frequency*duration)
            candidates=[w for w in (4,2) if len(raw)%w==0]
            width=min(candidates,key=lambda w:abs(len(raw)//w-expected)) if candidates else 0
        elif len(raw)%4==0:
            width=4
        if not width:
            raise ValueError('PPG 无法确定有效的 uint16/uint32 采样格式')
        values=np.frombuffer(raw,dtype='<u4' if width==4 else '<u2')
        counts.append(len(values))
        channels[key]=(name,'ADC',values)
    if not counts or len(set(counts))!=1 or acc_count and acc_count!=counts[0]:
        raise ValueError('PPG 光学与加速度通道样本数不一致')
    count=counts[0]
    if frequency<=0 and duration>0:
        frequency=count/duration
    if frequency<=0:
        raise ValueError('PPG 缺少采样率或采样时长，不能推造时间轴')
    if acc_count:
        samples=np.frombuffer(acc,dtype='<i2').reshape(-1,3).astype(float)/4096
        for i,axis in enumerate('xyz'):
            channels['a'+axis]=('加速度 '+axis.upper(),'g',samples[:,i])
    return PPGData(Path(source_path),int(data.get('create_time') or 0),frequency,
                   np.arange(count,dtype=float)*1000/frequency,channels,str(data.get('device','')))


def load_sensor_json(path, *, kind=None, acc_scale=4096):
    path=Path(path)
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    return parse_sensor_object(data, path, kind=kind, acc_scale=acc_scale)


def parse_sensor_object(data, path, *, kind=None, acc_scale=4096):
    if kind=='ppg' or 'imu' not in data and any(k in data for k in ('data','ir_data')):
        return parse_ppg_object(data,path)
    from cowmata_tailring.annotation.data import parse_motion_object
    return parse_motion_object(data,source_path=path,acc_scale=acc_scale)
