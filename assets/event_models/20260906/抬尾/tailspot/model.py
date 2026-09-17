# -*- coding: utf-8 -*-
import pickle
import numpy as np,pandas as pd
from threadpoolctl import threadpool_limits

def load_model(path):
    with open(path,'rb') as f:return pickle.load(f)
def predict(model,x):
    if not len(x):return np.empty(0)
    if not set(model['features'])<=set(x):raise ValueError('Model feature schema mismatch')
    with threadpool_limits(limits=4):
        a=model['imputer'].transform(x[model['features']]);return np.mean([q.predict_proba(a)[:,1] for q in model['models']],axis=0)
def nms(meta,score,scope='reference',separation=15):
    score=np.asarray(score);mask=meta.reference_clean.values if scope=='reference' else meta.observed_usable.values;chosen=[]
    for key,g in meta.groupby('session_key',sort=False):
        idx=g.index.values;times=g.spot_s.values;order=np.argsort(-score[idx],kind='stable');blocked=np.zeros(len(g),bool)
        for j in order:
            if blocked[j] or not mask[idx[j]] or not np.isfinite(score[idx[j]]):continue
            blocked|=np.abs(times-times[j])<separation;chosen.append(idx[j])
    out=meta.loc[chosen,['candidate_id','session_key','cow_group','spot_s','reference_clean']].copy();out['score']=score[chosen]
    return out.reset_index(drop=True)
def infer(record,model,budget=96,scope='reference',threshold=None):
    score=predict(model,record['features']);peaks=nms(record['meta'],score,scope)
    if threshold is None:threshold=float(model['training_cv_thresholds'][str(budget)])
    alerts=format_alerts(peaks[peaks.score>=threshold],record['create_ms'],threshold,scope)
    return alerts,peaks,score

def format_alerts(peaks,create,threshold,scope):
    alerts=peaks.copy();alerts['event']='TAIL_RAISED';alerts['status']='pending_video_review';alerts['score_is_probability']=False
    alerts['threshold']=threshold;alerts['quality_scope']=scope;alerts['event_wall_ms']=np.nan if create is None else create+alerts.spot_s*1000
    if create is None:alerts['hint']=['该记录约'+str(int(t//60))+'分附近疑似抬尾' for t in alerts.spot_s]
    else:alerts['hint']=[pd.to_datetime(int(create+t*1000),unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M')+'附近疑似抬尾' for t in alerts.spot_s]
    return alerts
