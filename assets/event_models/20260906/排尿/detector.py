"""Label-free JSON inference. All candidate decisions use only the preceding 60 s."""
from pathlib import Path
import json,pickle,hashlib,time
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from decoder import load_v2_json
from features import bins_1s
from windows import extract
from quality import window_quality
from alerts import decode

def load_model(path):
    # Load only supplied/trusted model files; this package never downloads executable models.
    with Path(path).open('rb') as f:pack=pickle.load(f)
    for key in ['model','feature_columns','trigger']:assert key in pack
    return pack

def prepare_json(path,cow,session_key=None):
    began=time.perf_counter();src=Path(path).resolve();s=load_v2_json(src)
    if s.create_time_ms is None:raise ValueError('MISSING_ABSOLUTE_CREATE_TIME')
    t=(s.elapsed_ms-s.elapsed_ms[0])/1000
    if np.any(np.diff(t)<=0):raise ValueError('NON_INCREASING_DEVICE_TIME')
    if s.diagnostics.dropped_prefix_bytes or s.diagnostics.dropped_trailing_bytes:raise ValueError('PROTOCOL_RECOVERY_REQUIRES_REVIEW')
    if t[-1]>86400:raise ValueError('RECORDING_GT_24H_REQUIRES_CHUNKING_REVIEW')
    b=bins_1s(t,s.raw_values);w=extract(b);w['session_key']=session_key or str(src);w['cow_group']=str(cow)
    q=window_quality(t,s.raw_values,w.end_s.values.astype(float)) if len(w) else pd.DataFrame(columns=['signal_clean'])
    quiet=(b.gyro<5)&(np.sqrt(b.ax_sd**2+b.ay_sd**2+b.az_sd**2)/4096<.03)&(b.coverage>=.98)
    norm=np.sqrt(b.ax**2+b.ay**2+b.az**2)/4096
    qnorm=float(norm[quiet].median()) if quiet.any() else None
    info=dict(raw_path=str(src),raw_sha256=hashlib.sha256(src.read_bytes()).hexdigest(),device_id=s.device,
        cow_group=str(cow),session_key=session_key or str(src),create_time_ms=s.create_time_ms,
        frames=len(t),sensor_duration_s=float(t[-1]),first_window_end_s=60,windows=len(w),
        raw_quality_accepted_windows=int(q.signal_clean.sum()),raw_quality_rejected_windows=int((~q.signal_clean.astype(bool)).sum()),
        quiet_seconds=int(quiet.sum()),quiet_norm_g=qnorm,strict_calibration_screen_pass=bool(qnorm is not None and quiet.sum()>=30 and .9<=qnorm<=1.1),
        preparation_seconds=time.perf_counter()-began,score_is_calibrated_probability=False)
    return w,q,info

def score_prepared(w,q,info,pack,threads=2):
    begin=time.perf_counter();columns=pack['feature_columns']
    if len(w):
        with threadpool_limits(limits=threads):scores=pack['model'].predict_proba(w[columns].values.astype(np.float32))[:,1]
        good=np.flatnonzero(q.signal_clean.values)
        runs=np.split(good,np.flatnonzero(np.diff(good)>1)+1)
        parts=[decode(w.iloc[ii].reset_index(drop=True),scores[ii],pack['trigger']) for ii in runs if len(ii)]
        prompts=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame(columns=['session_key','cow_group','notice_s','event_time_s','score'])
    else:scores=np.array([]);prompts=pd.DataFrame(columns=['session_key','cow_group','notice_s','event_time_s','score'])
    for c in ['raw_path','device_id']:prompts[c]=info[c]
    prompts['approx_time_bj']=pd.to_datetime(info['create_time_ms']+1000*prompts.event_time_s,unit='ms',utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m-%d %H:%M')
    prompts['message']='牛 '+info['cow_group']+' · '+prompts.approx_time_bj+' 附近疑似发生排尿'
    prompts['status']='UNKNOWN_REQUIRES_VIDEO';prompts['review_verdict']=''
    prompts['calibration_note']='STRICT_SCREEN_PASS_NOT_CALIBRATED' if info['strict_calibration_screen_pass'] else 'CALIBRATION_PENDING'
    prompts['profile']=pack.get('profile',pack.get('training_kind','unspecified'))
    # Absolute times are provenance only; public output is a point, not an inferred event duration.
    prompts['event_time_unix_ms']=info['create_time_ms']+1000*prompts.event_time_s
    return prompts,scores,time.perf_counter()-begin

def scan_json(path,cow,pack,session_key=None,threads=2):
    w,q,info=prepare_json(path,cow,session_key);p,sc,elapsed=score_prepared(w,q,info,pack,threads)
    info.update(prompts=len(p),inference_seconds=elapsed,status='UNLABELLED_CANDIDATES_UNTIL_EXTERNAL_EVALUATION')
    return p,info,q
