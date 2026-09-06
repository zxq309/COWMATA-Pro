"""Vectorized causal windows, algebraically identical to the audited feature formulas."""
import numpy as np
import pandas as pd
from features import geometry_features

def extract(b):
    ends=np.arange(60,len(b)+1,5)
    if not len(ends):return pd.DataFrame(columns=['end_s','center_s'])
    ci=ends[:,None]-30+np.arange(30);pi=ci-30
    coverage=b.coverage.values;ok=(coverage[ci].mean(1)>=.9)&(coverage[pi].mean(1)>=.9)
    ends=ends[ok];ci=ci[ok];pi=pi[ok]
    if not len(ends):return pd.DataFrame(columns=['end_s','center_s'])
    f={};units=b[['ux','uy','uz']].values;uu=units[ci];pp=units[pi]
    refs=np.median(pp,axis=1);refs/=np.maximum(np.linalg.norm(refs,axis=1,keepdims=True),1e-9)
    theta=np.degrees(np.arccos(np.clip(np.einsum('ijk,ik->ij',uu,refs),-1,1)))
    for j,c in enumerate('xyz'):
        f['unit_'+c+'_mean']=uu[:,:,j].mean(1);f['unit_'+c+'_std']=uu[:,:,j].std(1)
        f['unit_'+c+'_range']=np.ptp(uu[:,:,j],axis=1);f['unit_'+c+'_delta']=uu[:,:,j].mean(1)-refs[:,j]
    f['angle_mean']=theta.mean(1);f['angle_p90']=np.quantile(theta,.9,axis=1);f['angle_std']=theta.std(1)
    f['hold20']=(theta>20).mean(1);f['hold40']=(theta>40).mean(1)
    for j in range(3):f[f'angle_phase{j}']=theta[:,j*10:(j+1)*10].mean(1)
    for name in ['dynamic','gyro','mag']:
        y=b[name].values[ci];pre=b[name].values[pi]
        for name2,val in [('mean',y.mean(1)),('std',y.std(1)),('p90',np.quantile(y,.9,axis=1)),('max',y.max(1))]:f[name+'_'+name2]=val
        f[name+'_logratio']=np.log((y.mean(1)+.001)/(pre.mean(1)+.001))
        yc=y-y.mean(1)[:,None];pw=np.abs(np.fft.rfft(yc,axis=1))**2;freq=np.fft.rfftfreq(30,1)
        f[name+'_lowfreq_fraction']=pw[:,(freq>0)&(freq<.2)].sum(1)/np.maximum(pw.sum(1),1e-9)
        f[name+'_lag1']=(yc[:,:-1]*yc[:,1:]).sum(1)/np.maximum((yc*yc).sum(1),1e-9)
    base=pd.DataFrame(f);geo=geometry_features(b,ends).add_prefix('geo_')
    w=pd.concat([base,geo],axis=1);w['end_s']=ends;w['center_s']=ends-15
    return w
