# -*- coding: utf-8 -*-
"""Signal-only multiscale tail-ring point features. No annotated boundary inputs.

Raw samples are untouched. Means/covariances summarize observed one-second bins;
they never interpolate a gap. Adjacent-vector features require both bins valid.
"""
import numpy as np
import pandas as pd
from scipy.signal import find_peaks
import warnings
warnings.filterwarnings('ignore',category=RuntimeWarning)

def unit(x):return x/np.maximum(np.linalg.norm(x,axis=-1,keepdims=True),1e-8)
def angle(a,b):return np.degrees(np.arccos(np.clip(np.sum(a*b,axis=-1),-1,1)))
def mean_at(x,t,a,b):
    n=len(x);lo=np.clip(t+a,0,n);hi=np.clip(t+b,0,n)
    mask=np.isfinite(x);s=np.concatenate([np.zeros((1,)+x.shape[1:]),np.cumsum(np.where(mask,x,0),axis=0)])
    count=np.concatenate([np.zeros((1,)+x.shape[1:]),np.cumsum(mask,axis=0)])
    k=count[hi]-count[lo];v=(s[hi]-s[lo])/np.maximum(k,1);v[k==0]=np.nan
    return v,k/(b-a)
def features(b,t):
    valid=b.valid_bin.to_numpy();u=b[['ux','uy','uz']].to_numpy().copy();u[~valid]=np.nan
    gv=b[['gx','gy','gz']].to_numpy()/32;gv[~valid]=np.nan
    dyn=b.dynamic.to_numpy().copy();dyn[~valid]=np.nan
    gyro=b.gyro.to_numpy().copy();gyro[~valid]=np.nan
    norm=np.linalg.norm(b[['ax','ay','az']].to_numpy(),axis=1);norm[~valid]=np.nan
    d={};blocks={'pre':(-10,-3),'core':(-3,3),'post':(3,6),'late':(6,10),'longpre':(-25,-12),'longpost':(10,16)};refs={};noise={}
    for name,(lo,hi) in blocks.items():
        m,c=mean_at(u,t,lo,hi);refs[name]=unit(m)
        # Direction concentration converted to angular noise (no absolute g claim).
        noise[name]=np.degrees(np.sqrt(np.maximum(0,2*(1-np.linalg.norm(m,axis=1)))))
        d[name+'_orientation_noise']=noise[name];d[name+'_coverage']=c[:,0]
        d[name+'_tilt']=np.degrees(np.arccos(np.clip(-refs[name][:,2],-1,1)))
        for nm,v in [('gyro',gyro),('dynamic',dyn),('acc_norm',norm)]:
            avg,_=mean_at(v,t,lo,hi);sq,_=mean_at(v*v,t,lo,hi)
            d[name+'_'+nm]=avg;d[name+'_'+nm+'_sd']=np.sqrt(np.maximum(0,sq-avg*avg))
        g,_=mean_at(gv,t,lo,hi)
        d[name+'_gyro_directionality']=np.linalg.norm(g,axis=1)/np.maximum(d[name+'_gyro'],1)
        d[name+'_gyro_gravity_fraction']=np.abs((g*refs[name]).sum(1))/np.maximum(d[name+'_gyro'],1)
    for pre,post,prefix in [('pre','post','near'),('pre','late','keep'),('longpre','longpost','long')]:
        ag=angle(refs[pre],refs[post]);sg=np.sign(refs[post][:,2]-refs[pre][:,2]);den=np.maximum(.5,np.maximum(noise[pre],noise[post]))
        d[prefix+'_angle']=ag;d[prefix+'_signed_angle']=ag*sg;d[prefix+'_snr']=ag/den;d[prefix+'_signed_snr']=ag*sg/den
        d[prefix+'_tilt_step']=d[post+'_tilt']-d[pre+'_tilt'];d[prefix+'_z_step']=refs[post][:,2]-refs[pre][:,2]
        d[prefix+'_gyro_ratio']=np.log((d[post+'_gyro']+1)/(d[pre+'_gyro']+1))
        d[prefix+'_dynamic_ratio']=np.log((d[post+'_dynamic']+.001)/(d[pre+'_dynamic']+.001))
        d[prefix+'_norm_ratio']=d[post+'_acc_norm']/np.maximum(d[pre+'_acc_norm'],1)
    d['retention_angle']=angle(refs['post'],refs['late'])
    d['persistent_signed_angle']=np.minimum(d['near_signed_angle'],d['keep_signed_angle'])
    d['persistent_signed_snr']=np.minimum(d['near_signed_snr'],d['keep_signed_snr'])
    d['motion_relative_background']=np.log((d['core_gyro']+1)/(d['pre_gyro']+1))
    d['dynamic_relative_background']=np.log((d['core_dynamic']+.001)/(d['pre_dynamic']+.001))
    # Ordered 2-second samples represent a brief burst and return/retention.
    for a,z in [(-6,-4),(-4,-2),(-2,0),(0,2),(2,4),(4,6),(6,8),(8,10)]:
        suffix='m'+str(-a) if a<0 else 'p'+str(a)
        g,_=mean_at(gv,t,a,z);ur,_=mean_at(u,t,a,z);ur=unit(ur)
        v,_=mean_at(gyro,t,a,z);dy,_=mean_at(dyn,t,a,z)
        d['seq_gyro_'+suffix]=v;d['seq_dynamic_'+suffix]=dy
        d['seq_angle_'+suffix]=angle(refs['pre'],ur)
        d['seq_signed_angle_'+suffix]=d['seq_angle_'+suffix]*np.sign(ur[:,2]-refs['pre'][:,2])
        d['seq_gyro_vertical_'+suffix]=np.abs((g*ur).sum(1))/np.maximum(v,1)
    # Rotation-invariant covariance eigenvalues and vector geometry.
    for nm,v in [('u',u),('g',gv)]:
        avg,_=mean_at(v,t,-3,3);mom=np.zeros((len(t),3,3))
        for i in range(3):
            for j in range(3):mom[:,i,j]=mean_at(v[:,i]*v[:,j],t,-3,3)[0]-avg[:,i]*avg[:,j]
        eig=np.linalg.eigvalsh(np.nan_to_num(mom));eig=np.maximum(eig,0);total=np.maximum(eig.sum(1),1e-8)
        d['invariant_'+nm+'_linearity']=eig[:,2]/total;d['invariant_'+nm+'_planarity']=(eig[:,2]-eig[:,1])/total
    # Adjacent observed directions only; no derivative across missing seconds.
    turn=np.r_[np.nan,angle(u[1:],u[:-1])];sign=np.r_[np.nan,np.sign(np.diff(u[:,2]))];signed=turn*sign
    d['invariant_path']=mean_at(turn,t,-3,3)[0]*6
    d['signed_path']=mean_at(signed,t,-3,3)[0]*6
    d['invariant_efficiency']=d['keep_angle']/np.maximum(d['invariant_path'],1)
    d['signed_efficiency']=d['persistent_signed_angle']/np.maximum(d['invariant_path'],1)
    return pd.DataFrame(d)
