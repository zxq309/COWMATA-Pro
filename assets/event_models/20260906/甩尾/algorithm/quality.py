"""Score-independent native-sample quality checks. Never repairs raw values."""
import numpy as np

def mask_runs(x):
    d=np.diff(np.r_[False,x,False].astype(np.int8))
    return zip(np.flatnonzero(d==1),np.flatnonzero(d==-1))

def audit_session(session, expected_device=''):
    t=session.elapsed_ms-session.elapsed_ms[0];raw=session.raw_values;dt=np.diff(t)
    clipped=np.any(np.abs(raw[:,:6].astype(np.int32))>=32760,axis=1)
    zero=np.all(raw[:,:6]==0,axis=1);frozen=np.zeros(len(t),bool)
    for lo,hi in mask_runs(np.all(raw[1:]==raw[:-1],axis=1)):
        if t[hi]-t[lo]>=1000:frozen[lo:hi+1]=True
    bad=clipped|zero|frozen
    diagnostic=session.diagnostics
    reasons=[]
    if not (dt>0).all(): reasons.append('nonmonotonic_time')
    if diagnostic.phase_score<.95 or diagnostic.dropped_trailing_bytes: reasons.append('payload_structure')
    if session.create_time_ms is None: reasons.append('missing_create_time')
    if expected_device and not session.device.upper().endswith(expected_device.upper()): reasons.append('device_folder_mismatch')
    if len(t)<50: reasons.append('too_short')
    norm=np.nan;quiet=np.zeros(0,bool)
    if (dt>0).all() and len(t)>=50:
        _,sec=np.unique(t//1000,return_inverse=True);n=sec[-1]+1;cnt=np.bincount(sec,minlength=n)
        am=np.stack([np.bincount(sec,weights=raw[:,j].astype(float),minlength=n)/np.maximum(cnt,1) for j in range(3)],1)
        aa=np.sum(raw[:,:3].astype(float)**2,axis=1);gg=np.sum((raw[:,3:6].astype(float)/32)**2,axis=1)
        ar=np.sqrt(np.maximum(0,np.bincount(sec,weights=aa,minlength=n)/np.maximum(cnt,1)-np.sum(am*am,1)))
        gn=np.sqrt(np.bincount(sec,weights=gg,minlength=n)/np.maximum(cnt,1));an=np.linalg.norm(am,axis=1)
        cadence=np.r_[False,(dt<=0)|(dt>40)]
        secbad=np.bincount(sec,weights=(bad|cadence).astype(float),minlength=n)>0
        quiet=(cnt>=48)&(gn<5)&(ar/np.maximum(an,1)<.015)&~secbad
        if quiet.sum()>=20:norm=float(np.median(an[quiet])/4096)
    if quiet.sum()<20:reasons.append('insufficient_quiet_evidence')
    elif not .9<=norm<=1.1:reasons.append('quiet_acc_scale_outside_0.9_1.1g')
    # A reset/wrap can make end-minus-start negative. It is a diagnostic span,
    # never a valid monitoring-duration denominator for a rejected recording.
    quality=dict(core_eligible=not reasons,reject_reason=';'.join(reasons),
        duration_s=float(t[-1]/1000) if (dt>0).all() else None,device_signed_span_s=float(t[-1]/1000),
        device=session.device,create_time_ms=session.create_time_ms,first_device_elapsed_ms=int(session.elapsed_ms[0]),
        quiet_seconds=int(quiet.sum()),quiet_norm_nominal_g=norm,gaps_gt40ms=int((dt>40).sum()),
        gaps_gt100ms=int((dt>100).sum()),clipped_frames=int(clipped.sum()),zero_frames=int(zero.sum()),
        frozen_frames=int(frozen.sum()),**diagnostic.to_dict())
    return quality,t,bad

def clean_intervals(t,bad,start_ms,end_ms):
    start_ms=np.asarray(start_ms);end_ms=np.asarray(end_ms)
    left=np.searchsorted(t,start_ms);right=np.searchsorted(t,end_ms)
    inside=(start_ms>=t[0])&(end_ms<=t[-1]+20)&(right>left)
    badp=np.r_[0,np.cumsum(bad)]
    cadence=np.r_[False,(np.diff(t)<=0)|(np.diff(t)>40)]
    cp=np.r_[0,np.cumsum(cadence)]
    return inside&((badp[right]-badp[left])==0)&((cp[right]-cp[np.minimum(left+1,right)])==0)
