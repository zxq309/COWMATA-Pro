# -*- coding: utf-8 -*-
"""Tail-base ring event spotting. No labels are read by this module.

Primary output: fixed-threshold, bounded-lookahead event points.
Optional ranked mode is an OFFLINE review workload comparison only.
"""
from pathlib import Path
import argparse,hashlib,json
import numpy as np,pandas as pd,joblib
from scipy.signal import find_peaks
from .raw_io import load_v2_json
from .features import features
PACKAGE=Path(__file__).resolve().parents[1]
PEAK_LOOKAROUND_S=4.
def feature_columns(x):
    return [c for c in x if not c.startswith(('long','mag_','seq_','invariant_'))]
def bins_1s(t,raw):
    sec=np.floor(t).astype(int);n=int(sec[-1])+1;cnt=np.bincount(sec,minlength=n).astype(float);v={}
    for j,c in enumerate(['ax','ay','az','gx','gy','gz','mx','my','mz']):
        y=raw[:,j].astype(float);mu=np.bincount(sec,weights=y,minlength=n)/np.maximum(cnt,1)
        v[c]=mu;v[c+'_sd']=np.sqrt(np.maximum(0,np.bincount(sec,weights=y*y,minlength=n)/np.maximum(cnt,1)-mu*mu))
    norm=np.sqrt(sum(v[c]**2 for c in ['ax','ay','az']))
    for c,a in zip('xyz',['ax','ay','az']):v['u'+c]=v[a]/np.maximum(norm,1)
    v['dynamic']=np.sqrt(sum(v[c+'_sd']**2 for c in ['ax','ay','az']))/np.maximum(norm,1)
    v['gyro']=np.sqrt(sum(v[c]**2+v[c+'_sd']**2 for c in ['gx','gy','gz']))/32
    v['coverage']=np.minimum(cnt/50,1);v['second']=np.arange(n);bad=v['coverage']<.8
    for j in np.flatnonzero(np.diff(t)>.1):bad[int(t[j]):int(t[j+1])+1]=True
    six=raw[:,:6].astype(np.int32);hard=np.any(np.abs(six)>=32760,axis=1)|np.all(six==0,axis=1)
    same=np.all(six[1:]==six[:-1],axis=1)&(np.diff(t)<.1)
    edge=np.diff(np.r_[False,same,False].astype(int))
    for l,h in zip(np.flatnonzero(edge==1),np.flatnonzero(edge==-1)):
        if t[h]-t[l]>=.5:hard[l:h+1]=True
    bad[np.unique(sec[hard])]=True;v['valid_bin']=~bad
    return pd.DataFrame(v),int(hard.sum())
def extract(session,key='input',cow='unknown'):
    t=(session.elapsed_ms-session.elapsed_ms[0])/1000.
    if len(t)<100:raise ValueError('Too few raw samples for a usable record.')
    if np.any(np.diff(t)<=0):raise ValueError('Non-increasing timestamps: do not silently sort or interpolate.')
    b,hard=bins_1s(t,session.raw_values);n=len(b);v=np.where(b.valid_bin,b.gyro,0)
    active=pd.Series(v).rolling(5,center=True,min_periods=1).max().to_numpy()>8
    grid=np.arange(2,max(2,n-10),2);grid=grid[active[grid]] if len(grid) else grid
    peaks=find_peaks(v,height=12,distance=4)[0];centers=np.unique(np.r_[grid,peaks[(peaks>=2)&(peaks<n-10)]])
    x=features(b,centers);good=(x.core_coverage>=.5)&(x.post_coverage>=.5)&(x.late_coverage>=.5)&(x.pre_coverage>=2/7)
    x=x[good].reset_index(drop=True);centers=centers[good]
    meta=pd.DataFrame(dict(candidate_id=['%s_%07d'%(key,z) for z in centers],session_key=key,cow_group=str(cow),spot_s=centers+.5,recording_end_s=float(t[-1])))
    quiet=b.valid_bin&(b.gyro<5);nom=float(np.median(np.linalg.norm(b.loc[quiet,['ax','ay','az']].to_numpy(),axis=1))/4096) if quiet.any() else None
    audit=dict(session_key=key,cow_group=str(cow),raw_frames=len(t),duration_s=float(t[-1]),raw_hard_bad_points=hard,invalid_bins=int((~b.valid_bin).sum()),gaps_gt100ms=int((np.diff(t)>.1).sum()),
        max_gap_s=float(np.diff(t).max()),nominal_quiet_norm=nom,calibration_risk=nom is None or not(.75<=nom<=1.25),candidate_count=len(x),**session.diagnostics.to_dict())
    return x.astype(np.float32),meta,audit
def predict(bundle,x):
    if not len(x):return np.empty(0)
    z=x[bundle['columns']].to_numpy(np.float32).copy();bad=~np.isfinite(z);z[bad]=np.broadcast_to(bundle['median'],z.shape)[bad]
    return bundle['model'].predict_proba(z)[:,1]
def local_peaks(meta,scores):
    d=meta.copy();d['score']=scores;d=d[np.isclose((d.spot_s-.5)%2,0)].sort_values(['session_key','spot_s']);parts=[]
    for key,g in d.groupby('session_key',sort=False):
        t=g.spot_s.to_numpy();s=g.score.to_numpy();keep=[]
        for i,z in enumerate(t):
            l=np.searchsorted(t,z-PEAK_LOOKAROUND_S);h=np.searchsorted(t,z+PEAK_LOOKAROUND_S,side='right')
            if i==l+int(np.argmax(s[l:h])):keep.append(i)
        q=g.iloc[keep].copy();q['available_s']=q.spot_s+10+PEAK_LOOKAROUND_S
        if 'recording_end_s' in q:q=q[q.available_s<=q.recording_end_s]
        parts.append(q)
    if not parts:return d.assign(available_s=pd.Series(dtype=float))
    return pd.concat(parts,ignore_index=True)
def threshold_alerts(peaks,threshold):
    p=peaks[peaks.score>=threshold].sort_values(['session_key','available_s']);keep=[]
    for key,g in p.groupby('session_key',sort=False):
        last=-np.inf
        for ix,r in g.iterrows():
            if r.spot_s-last<20:continue
            last=r.spot_s;keep.append(ix)
    return p.loc[keep].reset_index(drop=True)
def ranked(meta,scores):
    d=meta.copy();d['score']=scores;keep=[]
    for key,g in d.groupby('session_key',sort=False):
        used=[]
        for ix,r in g.sort_values(['score','spot_s'],ascending=[False,True],kind='stable').iterrows():
            if any(abs(r.spot_s-z)<20 for z in used):continue
            used.append(r.spot_s);keep.append(ix)
    return d.loc[keep].assign(available_s=lambda z:z.spot_s+10).reset_index(drop=True)
def decorate(pp,create_ms):
    pp=pp.copy();pp['event']='起立'
    if create_ms is not None:pp['approx_time_bj']=[pd.Timestamp(int(round((create_ms+t*1000)/10000)*10000),unit='ms',tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S') for t in pp.spot_s]
    return pp
def main():
    p=argparse.ArgumentParser(description='Read complete COWMATA JSON and emit approximate stand-up event points.')
    inp=p.add_mutually_exclusive_group(required=True);inp.add_argument('--raw');inp.add_argument('--manifest',help='CSV json_path,cow_group; relative paths are relative to the manifest directory')
    p.add_argument('--cow',default='unknown');p.add_argument('--model',default=str(PACKAGE/'models/model.joblib'));p.add_argument('--output',required=True);p.add_argument('--threshold',type=float)
    p.add_argument('--mode',choices=['threshold','ranked'],default='threshold');p.add_argument('--budget-per-24h',type=float,default=24)
    args=p.parse_args();bundle=joblib.load(args.model);threshold=bundle['threshold'] if args.threshold is None else args.threshold
    if not 0<=threshold<=1:raise ValueError('threshold must lie in [0,1]')
    if args.budget_per_24h<=0:raise ValueError('review budget must be positive')
    rows=pd.read_csv(args.manifest,dtype={'cow_group':str}).to_dict('records') if args.manifest else [dict(json_path=str(Path(args.raw).resolve()),cow_group=args.cow)]
    parts=[];audit=[];hours={}
    for row in rows:
        path=Path(row['json_path']);path=path if path.is_absolute() else Path(args.manifest).resolve().parent/path;key=row.get('session_key',hashlib.sha1(str(path).encode()).hexdigest()[:12]);cow=str(row['cow_group'])
        session=load_v2_json(path);x,m,q=extract(session,key,cow);score=predict(bundle,x)
        pp=threshold_alerts(local_peaks(m,score),threshold) if args.mode=='threshold' else ranked(m,score)
        pp=decorate(pp,session.create_time_ms);pp['calibration_risk']=q['calibration_risk'];pp['json_path']=str(path);parts.append(pp);audit.append(q);hours[cow]=hours.get(cow,0)+q['duration_s']/3600
    out=pd.concat(parts,ignore_index=True)
    if args.mode=='ranked':
        limited=[]
        for cow,g in out.groupby('cow_group'):limited.append(g.sort_values('score',ascending=False,kind='stable').head(int(np.ceil(hours[cow]*args.budget_per_24h/24))))
        out=pd.concat(limited,ignore_index=True) if limited else out.iloc[:0]
    path=Path(args.output);path.parent.mkdir(exist_ok=True,parents=True);out.to_csv(path,index=False,encoding='utf-8-sig')
    path.with_suffix('.audit.json').write_text(json.dumps(dict(mode=args.mode,threshold=threshold,labels_read=False,score_is_calibrated_probability=False,records=audit),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(records=len(rows),alerts=len(out),mode=args.mode,threshold=threshold,labels_read=False,output=str(path.resolve())),ensure_ascii=False))
if __name__=='__main__':main()
