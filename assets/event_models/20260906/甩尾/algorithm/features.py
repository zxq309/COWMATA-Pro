"""178 numeric features from a complete decoded recording, with no labels.

Compatibility: tail-clean-v1-original178. The native 4 s core + 1 s margins
must have 0 < dt <= 40 ms. Optional 8 s and preceding-baseline context is
separately gap-delimited at 100 ms, matching the frozen recommended model.
Missing longer context stays NaN; previous-sample grid never bridges >100 ms.
"""
from pathlib import Path
import json,warnings
import numpy as np
import pandas as pd
from .signal_math import causal_windows,shape_features
from .quality import clean_intervals

VERSION='tail-clean-v1-original178'
COLUMNS=json.loads(Path(__file__).with_name('feature_columns.json').read_text(encoding='utf-8'))
AUX=['gyro_rms','gyro_max','dynamic_rms','dynamic_max','gyro_logratio','dynamic_logratio',
     'gyro_acf_max','gyro_sign_changes_per_s','gyro_jerk_rms','mean_direction_shift_deg']

def grid_previous(t_ms,raw,bad_raw=None):
    t=t_ms/1000;grid=np.arange(0,t[-1]+.00001,.02)
    right=np.searchsorted(t,grid).clip(1,len(t)-1)
    bad=(t[right]-t[right-1]>.1)&(grid>t[right-1]+1e-7)&(grid<t[right]-1e-7)
    previous=np.searchsorted(t,grid,side='right')-1
    bad|=(previous<0)|(grid>t[-1])
    if bad_raw is not None:bad|=bad_raw[previous.clip(0)]
    a=raw[previous.clip(0)].astype(np.float32);a[bad]=np.nan
    return a,bad

def extract(t_ms,raw,bad_raw,key,cow):
    a,bad=grid_previous(t_ms,raw,bad_raw)
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore',message='Mean of empty slice')
        warnings.filterwarnings('ignore',message='All-NaN slice encountered')
        w=causal_windows({'causal_a':a},key,cow)
    if w.empty:return w,pd.DataFrame(columns=COLUMNS)
    proposal=np.zeros(len(w),bool)
    for _,g in w.groupby('segment_id'):
        if len(g)>2:
            z=g.gyro_rms.to_numpy();ix=g.index.to_numpy()
            local=(z[1:-1]>=z[:-2])&(z[1:-1]>z[2:])&(g.gyro_max.to_numpy()[1:-1]>=10)&(g.dynamic_rms.to_numpy()[1:-1]>=.005)
            proposal[ix[1:-1][local]]=True
    strict=clean_intervals(t_ms,bad_raw,(w.end_s.to_numpy()-5)*1000,(w.end_s.to_numpy()+1)*1000)
    w['proposal']=proposal;w['strict_clean']=strict
    g=w[w.proposal&w.strict_clean].copy()
    segment=np.cumsum(np.r_[True,bad[1:]!=bad[:-1]]);segment[bad]=-1
    en=np.rint(g.end_s.to_numpy()*50).astype(int);frame=pd.DataFrame(index=g.index)
    for seconds in [1,2,4,8]:
        n=seconds*50;good=(en>=n)&(segment[np.maximum(en-n,0)]==segment[en-1])&(segment[en-1]>=0)
        ids=np.flatnonzero(good);pieces=[]
        for off in range(0,len(ids),400):
            ii=ids[off:off+400];ix=en[ii,None]-n+np.arange(n)
            f=shape_features(a[ix,:3].astype(float),a[ix,3:6].astype(float)/32)
            pieces.append(pd.DataFrame({f's{seconds}_{c}':v for c,v in f.items()},index=g.index[ii]))
        if pieces:frame=frame.join(pd.concat(pieces))
    frame=frame.reindex(columns=[c for c in COLUMNS if c not in AUX and not c.startswith('ratio_')])
    for name in ['dynamic_rms','gyro_rms']:
        for first,last in [(1,4),(2,4),(4,8)]:
            frame[f'ratio_{name}_{first}_{last}']=np.log((frame[f's{first}_{name}']+.001)/(frame[f's{last}_{name}']+.001))
    # Match the historical float32 feature storage and then arcsinh transform.
    frame=frame.astype(np.float32)
    for c in AUX:frame[c]=g[c].astype(np.float32)
    frame=frame.reindex(columns=COLUMNS)
    if np.isinf(frame.to_numpy()).any():raise ValueError('Infinite feature value')
    return w,frame

def model_matrix(frame):
    return np.arcsinh(frame[COLUMNS].to_numpy(np.float32))
