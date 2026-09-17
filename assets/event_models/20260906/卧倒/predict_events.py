# -*- coding: utf-8 -*-
"""Shared label-free feature extraction and approximate-time output helpers.

Use predict_clean_events.py as the command-line entry point with quality gates.
"""
import numpy as np,pandas as pd
from signal_features import bin5,proposals,feat
from source_v2_io import load_v2_json
from point_features import point_features
from core import nms

def valid_seconds(tms):
    if len(tms)==0 or np.any(np.diff(tms)<=0):raise ValueError('Elapsed time must increase strictly; split clock resets first.')
    sec=(tms//1000).astype(int);cnt=np.bincount(sec,minlength=int(sec[-1])+1);good=cnt>=40;t=tms/1000
    for i in np.flatnonzero(np.diff(t)>.1):good[int(t[i]):int(t[i+1])+1]=False
    return good

def extract(session,bundle,key='recording',cow='new'):
    t=session.elapsed_ms-session.elapsed_ms[0];raw=session.raw_values;good=valid_seconds(t)
    original=bin5(t,raw,good);boxes=proposals(original);s=original.copy()
    cal=bundle.get('calibration')
    if session.device.upper()=='546C50CA01F1' and cal is not None:
        corrected=raw.astype(float);corrected[:,:3]-=np.asarray(cal['offset_g'])*4096;s=bin5(t,corrected,good)
    mag=s['mag'].copy();s['mag']=np.c_[mag[:,1],-mag[:,0],mag[:,2]]
    features=[];rows=[]
    for i,(l,r,a,b) in enumerate(boxes):
        f=feat(s,l,r,a,b);extra,spot=point_features(original,l,r,a,b);f.update(extra);features.append(f)
        rows.append(dict(candidate_id=f'P{i:06d}',session_key=key,cow_group=cow,spot_s=spot,start_s=l/5,end_s=r/5,segment_start_s=a/5,segment_end_s=b/5))
    X=pd.DataFrame(features,columns=bundle['features']).replace([np.inf,-np.inf],np.nan).astype(np.float32)
    return X,pd.DataFrame(rows),good

def output_rows(alerts,session):
    rows=[]
    for _,r in alerts.iterrows():
        approximate=int(np.round(r.spot_s/5)*5);wall=None
        if session.create_time_ms is not None:wall=pd.to_datetime(session.create_time_ms+approximate*1000,unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S')
        rows.append(dict(code='LYING_DOWN',around_s=approximate,around_wall_bj=wall,ranking_score=float(r.score),device=session.device,message='该时刻附近可能发生卧倒'))
    return pd.DataFrame(rows,columns=['code','around_s','around_wall_bj','ranking_score','device','message']).sort_values('around_s')
