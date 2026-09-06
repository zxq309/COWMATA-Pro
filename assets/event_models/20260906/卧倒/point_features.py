import numpy as np

def point_features(s,l,r,a,b):
    gyro=s['gyro'];dyn=s['dyn'];gv=s['gyrovec']
    energy=gyro[l:r]/50+np.minimum(dyn[l:r]/.12,10);peak=l+int(np.argmax(energy));spot=peak/5+.1
    f={'peak_phase_symmetric':(peak-l)/max(r-l,1),'peak_gyro':gyro[peak],'peak_dynamic':dyn[peak]}
    for w in [2,4,8]:
        pre=slice(max(a,peak-w*5),peak);post=slice(peak+1,min(b,peak+w*5+1))
        for name,v in [('gyro',gyro),('dynamic',dyn)]:
            before=v[pre];after=v[post]
            bm=float(np.median(before)) if len(before) else np.nan;am=float(np.median(after)) if len(after) else np.nan
            f[f'peak_{name}_before_{w}']=bm;f[f'peak_{name}_after_{w}']=am
            f[f'peak_{name}_settle_{w}']=np.log((am+.01)/(bm+.01))
        v=gv[max(a,peak-w*5):min(b,peak+w*5+1)]
        signed=v.sum(0)*.2;absarea=np.abs(v).sum(0)*.2
        for j,c in enumerate('xyz'):
            f[f'signed_gyro_{w}_{c}']=signed[j];f[f'gyro_directionality_{w}_{c}']=signed[j]/(absarea[j]+1)
    return f,spot

