"""Causal 50 Hz raw summaries and 216 tail-ring features. No EDA zero-phase filter."""
import numpy as np
import pandas as pd

def bins_1s(t,raw):
    # Causal raw summaries for a separate detector; no zero-phase EDA filter reused.
    b=np.floor(t).astype(int);n=int(b[-1])+1;cnt=np.bincount(b,minlength=n).astype(float)
    vals={}
    for j,name in enumerate(['ax','ay','az','gx','gy','gz','mx','my','mz']):
        y=raw[:,j].astype(float);mu=np.bincount(b,weights=y,minlength=n)/np.maximum(cnt,1)
        va=np.maximum(0,np.bincount(b,weights=y*y,minlength=n)/np.maximum(cnt,1)-mu*mu)
        vals[name]=mu;vals[name+'_sd']=np.sqrt(va)
    acc=np.stack([vals[c] for c in ['ax','ay','az']],1);norm=np.linalg.norm(acc,axis=1)
    for j,c in enumerate('xyz'):vals['u'+c]=acc[:,j]/np.maximum(norm,1)
    vals['dynamic']=np.sqrt(sum(vals[c+'_sd']**2 for c in ['ax','ay','az']))/np.maximum(norm,1)
    vals['gyro']=np.sqrt(sum(vals[c]**2+vals[c+'_sd']**2 for c in ['gx','gy','gz']))/32
    vals['mag']=np.sqrt(sum(vals[c]**2 for c in ['mx','my','mz']))
    vals['coverage']=np.minimum(cnt/50,1);vals['second']=np.arange(n)
    return pd.DataFrame(vals)
def window_features(current,pre):
    f={};u=current[['ux','uy','uz']].values;p=pre[['ux','uy','uz']].values
    ref=np.median(p,axis=0);ref/=max(np.linalg.norm(ref),1e-9)
    theta=np.degrees(np.arccos(np.clip(u@ref,-1,1)))
    for j,c in enumerate('xyz'):
        f['unit_'+c+'_mean']=u[:,j].mean();f['unit_'+c+'_std']=u[:,j].std()
        f['unit_'+c+'_range']=np.ptp(u[:,j]);f['unit_'+c+'_delta']=np.mean(u[:,j])-ref[j]
    f['angle_mean']=theta.mean();f['angle_p90']=np.quantile(theta,.9);f['angle_std']=theta.std()
    f['hold20']=np.mean(theta>20);f['hold40']=np.mean(theta>40)
    for j in range(3):f[f'angle_phase{j}']=np.mean(theta[j*10:(j+1)*10])
    for name in ['dynamic','gyro','mag']:
        y=current[name].values;b=pre[name].values
        for stat,val in [('mean',np.mean(y)),('std',np.std(y)),('p90',np.quantile(y,.9)),('max',np.max(y))]:f[name+'_'+stat]=val
        f[name+'_logratio']=np.log((np.mean(y)+.001)/(np.mean(b)+.001))
        yf=y-y.mean();pw=np.abs(np.fft.rfft(yf))**2;freq=np.fft.rfftfreq(len(y),1)
        f[name+'_lowfreq_fraction']=pw[(freq>0)&(freq<.2)].sum()/max(pw.sum(),1e-9)
        f[name+'_lag1']=np.dot(yf[:-1],yf[1:])/max(np.dot(yf,yf),1e-9)
    return f
def geometry_features(b, ends):
    """Only bins [end-60,end). Rotation-invariant norms, dot products and spectra.

    Constant vector offsets cancel in centered acc and mag differences. No
    fitted calibration or anatomical pitch claim is made. Gyro magnitude uses
    the nominal decoder /32; acceleration uses /4096 nominal scale.
    """
    idx=ends[:,None]-60+np.arange(60)
    a=b[['ax','ay','az']].values[idx]/4096.
    m=b[['mx','my','mz']].values[idx]
    gy=b.gyro.values[idx]
    dyn=np.sqrt((b[['ax_sd','ay_sd','az_sd']].values**2).sum(1))[idx]/4096.
    f={}
    for name,x,eps in [('acc',a,.01),('mag',m,1.)]:
        ref=x[:,:15].mean(1)
        # Fixed isotropic unit scale for acc; mag uses past variability to avoid field magnitude.
        scale=np.ones(len(x)) if name=='acc' else np.maximum(np.sqrt(((x[:,:15]-ref[:,None])**2).sum(2).mean(1)),5.)
        z=(x-ref[:,None])/scale[:,None,None]
        block=z.reshape(len(x),6,10,3).mean(2)
        for i in range(1,6):
            f[f'{name}_displacement_block{i}']=np.linalg.norm(block[:,i],axis=1)
            f[f'{name}_step_block{i}']=np.linalg.norm(block[:,i]-block[:,i-1],axis=1)
        for i in range(1,6):
            for j in range(i+1,6):
                aa=block[:,i];bb=block[:,j]
                f[f'{name}_direction_cos_{i}_{j}']=(aa*bb).sum(1)/np.maximum(np.linalg.norm(aa,axis=1)*np.linalg.norm(bb,axis=1),eps)
        for length in [10,20,30]:
            cur=z[:,-length:];norm=np.linalg.norm(cur,axis=2);cen=cur-cur.mean(1)[:,None]
            prefix=f'{name}{length}_'
            f[prefix+'shift_mean']=norm.mean(1);f[prefix+'shift_std']=norm.std(1)
            f[prefix+'shift_p90']=np.quantile(norm,.9,axis=1)
            f[prefix+'motion_rms']=np.sqrt((cen**2).sum(2).mean(1))
            dd=np.linalg.norm(np.diff(cur,axis=1),axis=2)
            f[prefix+'path']=dd.sum(1)
            f[prefix+'efficiency']=np.linalg.norm(cur[:,-1]-cur[:,0],axis=1)/np.maximum(dd.sum(1),eps)
            f[prefix+'step_p90']=np.quantile(dd,.9,axis=1)
            gram=np.einsum('nti,ntj->nij',cen,cen)/length
            eigen=np.linalg.eigvalsh(gram)
            f[prefix+'linearity']=eigen[:,-1]/np.maximum(eigen.sum(1),eps**2)
            f[prefix+'planarity']=(eigen[:,-1]+eigen[:,-2])/np.maximum(eigen.sum(1),eps**2)
            for q in [.05,.15,.3]:
                f[prefix+'hold_'+str(q)]=(norm>q).mean(1)
            pw=(np.abs(np.fft.rfft(cen,axis=1))**2).sum(2);freq=np.fft.rfftfreq(length)
            total=np.maximum(pw[:,1:].sum(1),1e-12)
            f[prefix+'lowfreq_fraction']=pw[:,(freq>0)&(freq<=.15)].sum(1)/total
            pp=pw[:,1:]/total[:,None]
            f[prefix+'spectral_entropy']=-(pp*np.log(pp+1e-12)).sum(1)/np.log(max(pp.shape[1],2))
    for name,x,eps in [('gyronorm',gy,.1),('dynamic_counts',dyn,.005)]:
        for length in [5,10,20,30,60]:
            cur=x[:,-length:];prefix=f'{name}{length}_'
            f[prefix+'mean']=cur.mean(1);f[prefix+'std']=cur.std(1)
            f[prefix+'max']=cur.max(1)
            f[prefix+'logratio']=np.log((cur.mean(1)+eps)/(x[:,:15].mean(1)+eps))
        blocks=x.reshape(len(x),6,10).mean(2)
        for i in range(6): f[f'{name}_phase{i}']=blocks[:,i]
        f[name+'_late_early_ratio']=np.log((blocks[:,5]+eps)/(blocks[:,3]+eps))
    return pd.DataFrame(f)
