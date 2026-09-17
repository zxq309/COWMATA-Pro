"""Locally vendored numeric functions; labels are absent from this module.
Provenance: historical COWMATA tail-wagging causal_windows / shape_features.
The 50 Hz previous-sample grid is an internal representation only.
"""
import numpy as np
import pandas as pd

def slices(mask):
    edge=np.diff(np.r_[False,mask,False].astype(np.int8))
    return zip(np.flatnonzero(edge==1),np.flatnonzero(edge==-1))

def causal_windows(s,key,cow):
    """4 s trailing windows, 1 s stride, up to preceding 10 s baseline.

    The entire current window must lie in one >100 ms-gap-delimited segment.
    Baseline uses only available past data in that segment (minimum 1 second;
    otherwise baseline-relative features are missing). No EDA filter is used.
    """
    raw=s['causal_a'];valid=np.isfinite(raw).all(1);seg=np.full(len(valid),-1,np.int32)
    for number,(l,r) in enumerate(slices(valid)):seg[l:r]=number
    ends=np.arange(200,len(raw)+1,50)
    good=(seg[ends-200]>=0)&(seg[ends-200]==seg[ends-1]);ends=ends[good]
    if not len(ends):return pd.DataFrame()
    results=[]
    for off in range(0,len(ends),400):
        en=ends[off:off+400];ci=en[:,None]-200+np.arange(200);bi=en[:,None]-700+np.arange(500)
        a=raw[ci,:3].astype(float);g=raw[ci,3:6].astype(float)/32;pre=raw[bi.clip(0),:6].astype(float)
        baseline_valid=(bi>=0)&(seg[bi.clip(0)]==seg[en-1,None])&(seg[bi.clip(0)]>=0)
        pre[~baseline_valid]=np.nan
        baseline_ok=baseline_valid.sum(1)>=50
        bnorm=np.nanmedian(np.linalg.norm(pre[:,:,:3],axis=2),axis=1)
        bnorm=np.where(baseline_ok,bnorm,np.median(np.linalg.norm(a,axis=2),axis=1))
        an=a/np.maximum(bnorm[:,None,None],1)
        centered=an-an.mean(1,keepdims=True);gc=g-g.mean(1,keepdims=True)
        amp=np.linalg.norm(centered,axis=2);gn=np.linalg.norm(g,axis=2)
        gp=np.linalg.norm(pre[:,:,3:6]/32,axis=2)
        pa=pre[:,:,:3]/np.maximum(bnorm[:,None,None],1);pdyn=np.sqrt(np.nanmean(np.sum((pa-np.nanmean(pa,axis=1,keepdims=True))**2,axis=2),axis=1))
        f={}
        for name,y in [('dynamic',amp),('gyro',gn)]:
            f[name+'_rms']=np.sqrt(np.mean(y*y,axis=1));f[name+'_p90']=np.quantile(y,.9,axis=1)
            f[name+'_max']=y.max(1);f[name+'_std']=y.std(1);f[name+'_crest']=y.max(1)/np.maximum(f[name+'_rms'],1e-6)
        f['dynamic_logratio']=np.log((f['dynamic_rms']+.001)/(pdyn+.001))
        f['gyro_logratio']=np.log((f['gyro_rms']+.1)/(np.sqrt(np.nanmean(gp*gp,axis=1))+.1))
        ref=np.nanmean(pa,axis=1);ref/=np.maximum(np.linalg.norm(ref,axis=1,keepdims=True),1e-9)
        cm=an.mean(1);cm/=np.maximum(np.linalg.norm(cm,axis=1,keepdims=True),1e-9)
        f['mean_direction_shift_deg']=np.degrees(np.arccos(np.clip(np.sum(ref*cm,axis=1),-1,1)))
        for name in ['dynamic_logratio','gyro_logratio','mean_direction_shift_deg']:f[name][~baseline_ok]=np.nan
        for j in range(3):
            f[f'acc_axis{j}_std']=an[:,:,j].std(1);f[f'gyro_axis{j}_std']=g[:,:,j].std(1)
        # Signed three-axis PSD, not the norm/envelope PSD.
        fq=np.fft.rfftfreq(200,.02);hann=np.hanning(200)[None,:,None]
        for name,y in [('acc',centered),('signed_gyro',gc)]:
            power=np.sum(np.abs(np.fft.rfft(y*hann,axis=1))**2,axis=2);power[:,0]=0
            total=power.sum(1);probs=power/np.maximum(total[:,None],1e-12)
            f[name+'_peak_hz']=fq[np.argmax(power,axis=1)]
            f[name+'_entropy']=-np.sum(probs*np.log(probs+1e-12),axis=1)/np.log(len(fq)-1)
            for lo,hi in [(.25,1),(1,3),(3,8),(8,20)]:
                f[f'{name}_band_{lo:g}_{hi:g}']=power[:,(fq>=lo)&(fq<hi)].sum(1)/np.maximum(total,1e-12)
            f[name+'_peak_fraction']=power.max(1)/np.maximum(total,1e-12)
        var=np.sum(gc*gc,axis=1);axis=np.argmax(var,axis=1);dominant=np.take_along_axis(gc,axis[:,None,None],axis=2)[:,:,0]
        ac=[]
        for lag in [10,15,20,25,35,50,75,100]:
            ac.append(np.sum(dominant[:,:-lag]*dominant[:,lag:],axis=1)/np.maximum(np.sum(dominant**2,axis=1),1e-9))
        f['gyro_acf_max']=np.max(ac,axis=0)
        f['gyro_sign_changes_per_s']=np.sum(np.diff(np.sign(dominant),axis=1)!=0,axis=1)/4
        f['gyro_jerk_rms']=np.sqrt(np.mean(np.sum(np.diff(g,axis=1)**2,axis=2),axis=1))*50
        end_s=en/50;center=end_s-2
        f.update(session_key=key,cow_group=str(cow),segment_id=seg[en-1],end_s=end_s,center_s=center)
        results.append(pd.DataFrame(f))
    return pd.concat(results,ignore_index=True)

def shape_features(a,g,fs=50):
    """Invariants under a common proper orthogonal rotation of all sensor axes."""
    n=a.shape[1];ac=a-a.mean(1,keepdims=True);gc=g-g.mean(1,keepdims=True)
    ar=np.sqrt(np.mean(np.sum(ac*ac,axis=2),axis=1));gr=np.sqrt(np.mean(np.sum(gc*gc,axis=2),axis=1))
    norm=np.median(np.linalg.norm(a,axis=2),axis=1)
    ref=a.mean(1);ref/=np.maximum(np.linalg.norm(ref,axis=1,keepdims=True),1e-9)
    f={}
    for name,y,rms in [('a',ac,ar),('g',gc,gr)]:
        yn=np.linalg.norm(y,axis=2);power=(y*y).sum((1,2))
        cov=np.einsum('nti,ntj->nij',y,y)/n;ev=np.linalg.eigvalsh(cov)
        for j in range(3):f[f'{name}_eigen_fraction{j}']=ev[:,j]/np.maximum(ev.sum(1),1e-9)
        f[name+'_crest']=yn.max(1)/np.maximum(rms,1e-9)
        f[name+'_kurtosis']=np.mean(yn**4,axis=1)/np.maximum(rms**4,1e-9)
        f[name+'_normalized_jerk']=np.sqrt(np.mean(np.sum(np.diff(y,axis=1)**2,axis=2),axis=1))/np.maximum(rms,1e-9)
        parallel=np.einsum('nti,ni->nt',y,ref)
        f[name+'_gravity_parallel_fraction']=np.sum(parallel**2,axis=1)/np.maximum(power,1e-9)
        for lag in [10,25,50]:
            if lag<n:f[f'{name}_acf_{lag}']=np.sum(y[:,:-lag]*y[:,lag:],axis=(1,2))/np.maximum(power,1e-9)
        freq=np.fft.rfftfreq(n,1/fs)
        ps=np.sum(np.abs(np.fft.rfft(y*np.hanning(n)[None,:,None],axis=1))**2,axis=2);ps[:,0]=0
        total=ps.sum(1);prob=ps/np.maximum(total[:,None],1e-9)
        f[name+'_spectral_entropy']=-np.sum(prob*np.log(prob+1e-12),axis=1)/np.log(max(2,len(freq)-1))
        f[name+'_peak_hz']=freq[np.argmax(ps,axis=1)]
        f[name+'_peak_fraction']=np.max(prob,axis=1)
        for lo,hi in [(.25,1),(1,3),(3,8),(8,20)]:f[f'{name}_band_{lo}_{hi}']=ps[:,(freq>=lo)&(freq<hi)].sum(1)/np.maximum(total,1e-9)
        f[name+'_front_back_energy_ratio']=np.log((np.mean(yn[:,:n//2]**2,axis=1)+1e-6)/(np.mean(yn[:,n//2:]**2,axis=1)+1e-6))
    f['dynamic_rms']=ar/np.maximum(norm,1)
    f['gyro_rms']=np.sqrt(np.mean(np.sum(g*g,axis=2),axis=1))
    f['gyro_cancellation']=np.linalg.norm(np.sum(g,axis=1),axis=1)/np.maximum(np.linalg.norm(g,axis=2).sum(1),1e-9)
    # Simultaneous translation-rotation coupling is invariant to shared mounting rotation.
    an=np.linalg.norm(ac,axis=2);gn=np.linalg.norm(gc,axis=2);an-=an.mean(1,keepdims=True);gn-=gn.mean(1,keepdims=True)
    f['acc_gyro_envelope_corr']=np.sum(an*gn,axis=1)/np.maximum(np.sqrt(np.sum(an*an,axis=1)*np.sum(gn*gn,axis=1)),1e-9)
    f['acc_gyro_rms_ratio']=ar/np.maximum(norm,1)/(gr+.1)
    return f
