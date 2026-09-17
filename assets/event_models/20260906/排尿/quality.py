import numpy as np
import pandas as pd

def window_quality(t,raw,ends):
    """All flags at end use only data before end. No future calibration is applied."""
    if len(t)<2 or np.any(np.diff(t)<=0):raise ValueError('TIME_ORDER_INVALID')
    n=max(int(np.floor(t[-1]))+1,int(np.max(ends)) if len(ends) else 0)
    sec=t.astype(int);idx=ends.astype(int)[:,None]-60+np.arange(60)
    a=np.searchsorted(t,ends-60);b=np.searchsorted(t,ends)
    coverage=np.minimum((b-a)/3000.,1.)
    gap=np.zeros(n);np.maximum.at(gap,sec,np.r_[0.,np.diff(t)])
    clip=np.bincount(sec,weights=np.any((raw==-32768)|(raw==32767),axis=1),minlength=n)
    zero=np.bincount(sec,weights=np.all(raw==0,axis=1),minlength=n)
    change=np.r_[True,np.any(raw[1:]!=raw[:-1],axis=1)]
    last=np.maximum.accumulate(np.where(change,t,0.))
    flat=np.bincount(sec,weights=t-last>=1,minlength=n)
    q=pd.DataFrame(dict(end_s=ends,coverage=coverage,max_gap_s=gap[idx].max(1),clipped_frames=clip[idx].sum(1),zero_frames=zero[idx].sum(1),frozen_frames=flat[idx].sum(1)))
    q['signal_clean']=(q.coverage>=.985)&(q.max_gap_s<=.30)&(q.clipped_frames==0)&(q.zero_frames==0)&(q.frozen_frames==0)
    return q
