# -*- coding: utf-8 -*-
"""JSON -> raw timestamps -> masked features and independent signal quality.

No annotation or old cache is opened by this module. Raw counts are retained;
>100 ms gaps are never interpolated or used in raw differences.
"""
from pathlib import Path
import json,base64,hashlib
import numpy as np
import pandas as pd
from .decoder import decode_v2_bytes,_phase_score

CHANNELS=['acc_x_counts','acc_y_counts','acc_z_counts','gyro_x_counts','gyro_y_counts','gyro_z_counts','mag_x_counts','mag_y_counts','mag_z_counts']
def unit(x):return x/np.maximum(np.linalg.norm(x,axis=-1,keepdims=True),1e-9)
def angle(x,y):return np.degrees(np.arccos(np.clip(np.sum(x*y,axis=-1),-1,1)))
def aggregate(x,l,r,mask):
    xx=np.nan_to_num(x)*mask[:,None];cs=np.vstack([np.zeros(xx.shape[1]),np.cumsum(xx,axis=0)]);cs2=np.vstack([np.zeros(xx.shape[1]),np.cumsum(xx*xx,axis=0)])
    cnt=np.r_[0,np.cumsum(mask)];n=cnt[r]-cnt[l];mean=(cs[r]-cs[l])/np.maximum(n[:,None],1)
    sd=np.sqrt(np.maximum(0,(cs2[r]-cs2[l])/np.maximum(n[:,None],1)-mean*mean));mean[n==0]=np.nan;sd[n==0]=np.nan
    return mean,sd,n/np.maximum(r-l,1)

def bins_1s(t_ms,v):
    sec=t_ms//1000;n=int(sec[-1])+1;count=np.bincount(sec,minlength=n).astype(float);vals={}
    for j,name in enumerate(['ax','ay','az','gx','gy','gz','mx','my','mz']):
        y=v[:,j].astype(float);mu=np.bincount(sec,weights=y,minlength=n)/np.maximum(count,1)
        vals[name]=mu;vals[name+'_sd']=np.sqrt(np.maximum(0,np.bincount(sec,weights=y*y,minlength=n)/np.maximum(count,1)-mu*mu))
    acc=np.stack([vals[c] for c in ['ax','ay','az']],1);norm=np.linalg.norm(acc,axis=1)
    for j,c in enumerate('xyz'):vals['u'+c]=acc[:,j]/np.maximum(norm,1)
    vals['dynamic']=np.sqrt(sum(vals[c+'_sd']**2 for c in ['ax','ay','az']))/np.maximum(norm,1)
    vals['gyro']=np.sqrt(sum(vals[c]**2+vals[c+'_sd']**2 for c in ['gx','gy','gz']))/32
    vals['mag']=np.sqrt(sum(vals[c]**2 for c in ['mx','my','mz']));vals['coverage']=np.minimum(count/50,1);vals['second']=np.arange(n)
    return pd.DataFrame(vals),count

def mechanics(t,v,n):
    v=v.astype(float);sec=t//1000;count=np.bincount(sec,minlength=n);g=v[:,3:6]/32;an=np.maximum(np.linalg.norm(v[:,:3],axis=1),1)
    dt=np.diff(t)/1000;ok=(dt>=.005)&(dt<=.1);k=sec[1:][ok]
    aj=np.linalg.norm(np.diff(v[:,:3],axis=0)[ok],axis=1)/((an[:-1][ok]+an[1:][ok])*.5)/dt[ok]
    gj=np.linalg.norm(np.diff(g,axis=0)[ok],axis=1)/dt[ok];den=np.maximum(np.bincount(k,minlength=n),1)
    a=np.sqrt(np.bincount(k,weights=aj*aj,minlength=n)/den);b=np.sqrt(np.bincount(k,weights=gj*gj,minlength=n)/den)
    mean=np.column_stack([np.bincount(sec,weights=g[:,j],minlength=n)/np.maximum(count,1) for j in range(3)]);second=np.zeros((n,3,3))
    for i in range(3):
        for j in range(i,3):second[:,i,j]=second[:,j,i]=np.bincount(sec,weights=g[:,i]*g[:,j],minlength=n)/np.maximum(count,1)
    cov=second-mean[:,:,None]*mean[:,None,:];eig=np.maximum(np.linalg.eigvalsh(cov),0)
    coherent=np.linalg.norm(mean,axis=1)/np.maximum(np.sqrt(np.trace(second,axis1=1,axis2=2)),1e-6)
    return np.column_stack([np.log1p(a),np.log1p(b),coherent,eig[:,-1]/np.maximum(eig.sum(1),1e-6)])

def quality(t,v,b,count,phase_score,phase_margin,binding_ok=True):
    sec=t//1000;n=len(b);dt=np.diff(t);sat=(np.abs(v.astype(np.int32))>=32760).any(1);zero=(v[:,:6]==0).all(1)
    same=np.r_[False,(np.diff(v[:,:6].astype(np.int32),axis=0)==0).all(1)];edge=np.diff(np.r_[False,same,False].astype(int));frozen=np.zeros(len(t),bool)
    for l,r in zip(np.flatnonzero(edge==1),np.flatnonzero(edge==-1)):
        if t[r-1]-t[max(l-1,0)]>=500:frozen[max(l-1,0):r]=True
    bad=sat|zero|frozen;badsec=np.bincount(sec,weights=bad,minlength=n)>0;maxgap=np.zeros(n);np.maximum.at(maxgap,sec[1:],dt/1000)
    for index in np.flatnonzero(dt>100):
        aa=int(t[index]//1000);bb=int(t[index+1]//1000);maxgap[aa:bb+1]=np.maximum(maxgap[aa:bb+1],dt[index]/1000)
    quiet=(b.dynamic.values<.02)&(np.sqrt((b[['gx_sd','gy_sd','gz_sd']].values**2).sum(1))/32<2)&(count>=40)&~badsec
    norms=np.linalg.norm(b[['ax','ay','az']].values[quiet],axis=1)/4096
    med=float(np.median(norms)) if len(norms) else None;iqr=float(np.subtract(*np.percentile(norms,[75,25]))) if len(norms) else None
    transport=bool((dt>0).all() and phase_score>=.95 and phase_margin>=.1 and binding_ok)
    reference=bool(transport and len(norms)>=60 and med is not None and .85<=med<=1.15 and iqr/max(med,1e-9)<=.1)
    good=(count>=40)&~badsec&(maxgap<=.1)
    summary=dict(transport_clean=transport,nominal_reference_clean=reference,quiet_seconds=int(quiet.sum()),static_norm_nominal_g=med,static_norm_iqr_g=iqr,
        bad_frames=int(bad.sum()),saturation_frames=int(sat.sum()),zero_frames=int(zero.sum()),frozen_frames=int(frozen.sum()),gap_count=int((dt>100).sum()),
        max_gap_s=float(dt.max()/1000),phase_score=float(phase_score),phase_margin=float(phase_margin),calibration_applied=False)
    return summary,good,badsec,bad

def dense_features(t,v,b,good,badsec,summary,key,cow):
    n=len(b);u=b[['ux','uy','uz']].values;mask=(b.coverage.values>=.5)&np.isfinite(u).all(1)&~badsec
    rr=mechanics(t,v,n);ys=np.column_stack([np.log1p(b.dynamic.values),np.log1p(b.gyro.values),np.log1p(np.maximum(b.mag.values,0)),rr])
    ynames=['dynamic','gyro','mag','acc_jerk','gyro_jerk','gyro_coherence','gyro_axis_fraction'];ix=np.arange(0,n,2);ix=ix[mask[ix]];f={}
    pre,_,pre_cov=aggregate(u,np.maximum(ix-25,0),np.maximum(ix-5,0),mask);post,_,post_cov=aggregate(u,np.minimum(ix+6,n),np.minimum(ix+26,n),mask)
    upre=unit(pre);upost=unit(post);f['pre_coverage']=pre_cov;f['post_coverage']=post_cov;f['closure_pre_post']=angle(upre,upost)
    for duration in [3,7,15,31]:
        h=duration//2;l=np.maximum(ix-h,0);r=np.minimum(ix+h+1,n);mean,sd,cov=aggregate(u,l,r,mask);mu=unit(mean)
        f[f'angle_{duration}_pre']=angle(mu,upre);f[f'angle_{duration}_post']=angle(mu,upost);f[f'orientation_dispersion_{duration}']=np.sqrt(np.maximum(0,2*(1-np.linalg.norm(mean,axis=1))))
        f[f'coverage_{duration}']=cov;f[f'available_fraction_{duration}']=(r-l)/duration;mm,ss,_=aggregate(ys,l,r,mask)
        for k,name in enumerate(ynames):f[f'{name}_{duration}_mean']=mm[:,k];f[f'{name}_{duration}_sd']=ss[:,k]
        for k,axis in enumerate('xyz'):f[f'direction_{axis}_{duration}_mean']=mean[:,k];f[f'direction_{axis}_{duration}_delta']=mean[:,k]-upre[:,k]
    pmean,_,_=aggregate(ys,np.maximum(ix-25,0),np.maximum(ix-5,0),mask)
    for k,name in enumerate(ynames):f[f'{name}_7_relative']=f[f'{name}_7_mean']-pmean[:,k]
    f['peak_over_closure']=f['angle_7_pre']/(1+f['closure_pre_post']);f['pulse_concentration']=f['angle_3_pre']/(1+f['angle_31_pre']);f['motion_vs_posture']=f['gyro_7_mean']/(1+f['angle_7_pre'])
    x=pd.DataFrame(f).astype(np.float32).add_prefix('legacy_');l=np.maximum(ix-10,0);r=np.minimum(ix+11,n);badcount=np.r_[0,np.cumsum(~good)]
    cont=(ix>=10)&(ix+11<=n)&((badcount[r]-badcount[l])==0)&summary['transport_clean']
    # Reject contaminated feature contexts. Gaps remain masked observations.
    bc=np.r_[0,np.cumsum(badsec)];observed=(bc[np.minimum(ix+26,n)]-bc[np.maximum(ix-25,0)]==0)&summary['transport_clean']
    m=pd.DataFrame(dict(candidate_id=[f'{key}:{k}' for k in ix],session_key=key,cow_group=str(cow),spot_s=ix+.5,
        continuous_clean=cont,reference_clean=cont&summary['nominal_reference_clean']&observed,observed_usable=observed,
        feature_left_s=np.maximum(ix-25,0),feature_right_s=np.minimum(ix+26,n)))
    return x,m

def read_json_features(path,cow='unknown',session_key=None,expected_device=None):
    path=Path(path);rawbytes=path.read_bytes();sha=hashlib.sha256(rawbytes).hexdigest();doc=json.loads(rawbytes.decode('utf-8-sig'))
    if not isinstance(doc.get('imu'),str):raise ValueError('JSON lacks Base64 imu string')
    payload=base64.b64decode(''.join(doc['imu'].split()),validate=True);device=str(doc.get('device',''));tm,v,diag=decode_v2_bytes(payload)
    t=tm-tm[0]
    if np.any(np.diff(t)<=0):raise ValueError('Non-increasing timestamps: file quarantined, no interpolation')
    scores=sorted([_phase_score(payload,k) for k in range(22)],reverse=True);b,count=bins_1s(t,v)
    qc,good,badsec,bad=quality(t,v,b,count,diag.phase_score,scores[0]-scores[1],expected_device is None or device==str(expected_device))
    key=session_key or sha[:12];x,m=dense_features(t,v,b,good,badsec,qc,key,cow)
    create=doc.get('create_time');create=int(create) if isinstance(create,(int,float)) else None
    qc.update(source_path=str(path.resolve()),source_sha256=sha,device=device,session_key=key,cow_group=str(cow),create_ms=create,frames=len(t),duration_s=float(t[-1]/1000),
        binding_checked=expected_device is not None,expected_device=expected_device,decoder_diagnostics=diag.to_dict(),
        candidates=len(m),reference_points=int(m.reference_clean.sum()),observed_points=int(m.observed_usable.sum()),source_read='full_json',old_cache_used=False)
    return dict(t_ms=t,device_t_ms=tm,raw=v,create_ms=create,features=x,meta=m,quality=qc,raw_bad=bad,good_seconds=good)
