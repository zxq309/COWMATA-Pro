import numpy as np
import pandas as pd
from numba import njit

@njit(cache=True)
def trigger_indices(ends,sessions,prob,threshold,support,cooldown,reset_fraction):
    out=np.empty(len(prob),np.int64);count=0;armed=True;last_alert=-1e12;low_count=0
    h0=h1=h2=0
    for i in range(len(prob)):
        if i==0 or sessions[i]!=sessions[i-1] or ends[i]-ends[i-1]>10:
            armed=True;last_alert=-1e12;low_count=0;h0=h1=h2=0
        if not np.isfinite(prob[i]):
            h0=h1=h2=0;low_count=0
            continue
        if not armed:
            low_count=low_count+1 if prob[i]<=threshold*reset_fraction else 0
            if low_count>=2 and ends[i]-last_alert>=cooldown:
                armed=True;h0=h1=h2=0
            continue
        h0=h1;h1=h2;h2=int(prob[i]>=threshold)
        if h2==1 and h0+h1+h2>=support:
            out[count]=i;count+=1;armed=False;last_alert=ends[i];low_count=0;h0=h1=h2=0
    return out[:count]

def decode(w,prob,config):
    # w must be in recording/time order. Probability array is local to w.
    codes=pd.factorize(w.session_key,sort=False)[0].astype(np.int64)
    ii=trigger_indices(w.end_s.values.astype(float),codes,np.asarray(prob),config['threshold'],
         config['support'],config['cooldown'],config['reset_fraction'])
    d=w.iloc[ii][['session_key','cow_group','end_s']].copy().reset_index(drop=True)
    d=d.rename(columns={'end_s':'notice_s'});d['event_time_s']=d.notice_s-15
    d['score']=np.asarray(prob)[ii]
    return d
