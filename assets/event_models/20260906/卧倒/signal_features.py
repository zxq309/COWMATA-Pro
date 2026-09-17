"""Signal-only 5Hz event proposals, rotation-relative geometry and phase features.

Raw 50Hz points are retained separately. No interpolation bridges a raw gap.
Use the frozen valid-second support for comparable predictions and exposure.
"""
import numpy as np,pandas as pd,json,warnings
from scipy.signal import find_peaks
def runs(mask):
    z=np.diff(np.r_[False,mask,False].astype(np.int8))
    return np.c_[np.flatnonzero(z==1),np.flatnonzero(z==-1)]
warnings.filterwarnings('ignore',category=RuntimeWarning)

def norm(a):return np.linalg.norm(a,axis=-1)
def unit(a):return a/np.maximum(norm(a)[...,None],1e-6)
def ang(a,b):return np.degrees(np.arccos(np.clip(np.sum(unit(a)*unit(b),axis=-1),-1,1)))

def bin5(tms,raw,valid_sec):
    b=(tms//200).astype(int);n=int(b[-1])+1;cnt=np.bincount(b,minlength=n)
    mu=[];sd=[]
    for j in range(9):
        y=raw[:,j].astype(float);m=np.bincount(b,weights=y,minlength=n)/np.maximum(cnt,1)
        vv=np.bincount(b,weights=y*y,minlength=n)/np.maximum(cnt,1)-m*m
        mu.append(m);sd.append(np.sqrt(np.maximum(vv,0)))
    mu=np.array(mu).T;sd=np.array(sd).T;a=mu[:,:3];u=unit(a);scale=np.maximum(norm(a),1)
    dyn=norm(sd[:,:3])/scale;gyr=np.sqrt((mu[:,3:6]**2+sd[:,3:6]**2).sum(1))/32
    valid=(cnt>=8)&valid_sec[np.minimum(np.arange(n)//5,len(valid_sec)-1)]
    return dict(t=np.arange(n)/5,u=u,dyn=dyn,gyro=gyr,mag=unit(mu[:,6:9]),
                gyrovec=mu[:,3:6]/32,axis_sd=sd[:,:3]/scale[:,None],valid=valid)

def proposals(s):
    found=set();dyn=s['dyn'];gyro=s['gyro']
    for a,b in runs(s['valid']):
        if b-a<5:continue
        for level in [.025,.06,.12]:
            mask=((dyn[a:b]>level)&(gyro[a:b]>8))|(gyro[a:b]>40)
            rr=runs(mask);joined=[]
            for l,r in rr:
                if joined and l-joined[-1][1]<=5:joined[-1][1]=r
                else:joined.append([l,r])
            for l,r in joined:
                l+=a;r+=a
                if r-l<4 or max(np.max(dyn[l:r])/.12,np.max(gyro[l:r])/40)<1:continue
                if r-l<=225:found.add((l,r,a,b))
                # A few signal peak proposals rescue boundaries inside long motion bouts.
                energy=np.maximum(dyn[l:r]/.12,gyro[l:r]/50)
                peaks,_=find_peaks(energy,distance=25,prominence=.75)
                if not len(peaks):peaks=np.array([np.argmax(energy)])
                peaks=peaks[np.argsort(energy[peaks])[-4:]]
                for p in peaks+l:
                    for w in [20,40,80]:
                        st=max(a,p-w);en=min(b,p+w)
                        if en-st>=5:found.add((st,en,a,b))
    return sorted(found)

def feat(s,l,r,segstart,segend):
    out={'duration_s':(r-l)/5};u=s['u'][l:r];pre=s['u'][max(segstart,l-30):l];post=s['u'][r:min(segend,r+30)]
    # Adapt short context to real available history; record its size. No bridging.
    pu=unit(np.mean(pre,axis=0)) if len(pre)>=3 else np.full(3,np.nan)
    qu=unit(np.mean(post,axis=0)) if len(post)>=3 else np.full(3,np.nan)
    theta=ang(u,pu);tpost=ang(u,qu);path=ang(u[1:],u[:-1])
    out.update(pre_available_s=len(pre)/5,post_available_s=len(post)/5,
               pre_post_angle=float(ang(pu,qu)),path_length_deg=float(path.sum()),
               path_efficiency=float(ang(pu,qu)/(path.sum()+1)),
               angle_pre_max=float(np.nanmax(theta)),angle_pre_mean=float(np.nanmean(theta)),
               angle_post_mean=float(np.nanmean(tpost)),angle_pre_p90=float(np.nanpercentile(theta,90)),
               angle_post_p90=float(np.nanpercentile(tpost,90)),turn_p90=float(np.percentile(path,90)))
    for j,c in enumerate('xyz'):
        out['pre_u'+c]=pu[j];out['post_u'+c]=qu[j];out['delta_u'+c]=qu[j]-pu[j]
        out['range_u'+c]=np.ptp(u[:,j]);out['median_u'+c]=np.median(u[:,j])
    for phase,(a,b) in enumerate(zip(np.linspace(l,r,6).astype(int)[:-1],np.linspace(l,r,6).astype(int)[1:])):
        x=s['u'][a:max(a+1,b)]
        out[f'phase{phase}_angle_pre']=float(np.nanmean(ang(x,pu)));out[f'phase{phase}_angle_post']=float(np.nanmean(ang(x,qu)))
        for j,c in enumerate('xyz'):out[f'phase{phase}_u{c}']=float(np.mean(x[:,j]))
        for name in ['dyn','gyro']:
            y=s[name][a:max(a+1,b)];out[f'phase{phase}_{name}']=float(np.mean(y))
    for name in ['dyn','gyro']:
        y=s[name][l:r];before=s[name][max(segstart,l-30):l];after=s[name][r:min(segend,r+30)]
        bm=np.median(before) if len(before)>=3 else np.nan;am=np.median(after) if len(after)>=3 else np.nan
        out[name+'_mean']=float(np.mean(y));out[name+'_p90']=float(np.percentile(y,90));out[name+'_max']=float(np.max(y))
        out[name+'_std']=float(np.std(y));out[name+'_peak_phase']=float(np.argmax(y)/len(y))
        out[name+'_pre_median']=bm;out[name+'_post_median']=am
        out[name+'_burst_logratio']=float(np.log((np.percentile(y,90)+.001)/(bm+.001)))
        out[name+'_settle_logratio']=float(np.log((am+.001)/(np.percentile(y,90)+.001)))
        centered=y-np.mean(y);p=np.abs(np.fft.rfft(centered))**2;fq=np.fft.rfftfreq(len(y),.2)
        total=p[1:].sum();pp=p[1:]/max(total,1e-9)
        out[name+'_spectral_centroid']=float(np.sum(fq*p)/max(total,1e-9))
        out[name+'_spectral_entropy']=float(-np.sum(pp*np.log(pp+1e-9))/np.log(max(len(pp),2)))
        out[name+'_low_band']=float(p[(fq>=.1)&(fq<.5)].sum()/max(total,1e-9))
    # Dot/cross relations are invariant to a common sensor rotation; magnetic changes
    # are exploratory, not assumed to be a calibrated compass or absolute body angle.
    gv=s['gyrovec'][l:r];out['gyro_parallel_pre_mean']=float(np.mean(gv@pu));out['gyro_parallel_post_mean']=float(np.mean(gv@qu))
    axis=unit(np.cross(pu,qu));out['gyro_transition_axis_mean']=float(np.mean(gv@axis))
    out['gyro_transition_axis_peak']=float(np.max(gv@axis));out['gyro_transition_axis_min']=float(np.min(gv@axis))
    mv=s['mag'][l:r];out['acc_mag_cos_mean']=float(np.mean(np.sum(u*mv,axis=1)))
    out['acc_mag_cos_range']=float(np.ptp(np.sum(u*mv,axis=1)))
    return out
