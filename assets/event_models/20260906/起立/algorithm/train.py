# -*- coding: utf-8 -*-
"""Train only curated dataset rows; no validation labels or old cache imports."""
from pathlib import Path
import json,sys,joblib
import numpy as np,pandas as pd
from sklearn.ensemble import ExtraTreesClassifier
PACKAGE=Path(__file__).resolve().parents[1];SEED=20260906
META=['candidate_id','session_key','cow_group','spot_s','parent_event_id','target']
def fit(table,excluded,seed):
    d=table[~table.cow_group.isin(excluded)].copy();cols=[c for c in table if c not in META];x=d[cols].replace([np.inf,-np.inf],np.nan);med=x.median().fillna(0).to_numpy(np.float32)
    a=x.to_numpy(np.float32);bad=~np.isfinite(a);a[bad]=np.broadcast_to(med,a.shape)[bad];y=d.target.to_numpy(int);w=np.zeros(len(d))
    for target in [0,1]:
        for cow,g in d[d.target==target].groupby('cow_group'):
            if target:
                sizes=g.groupby('parent_event_id').size();ww=g.parent_event_id.map(1/sizes).to_numpy()/len(sizes)
            else:ww=np.full(len(g),1/len(g))
            w[d.index.get_indexer(g.index)]=ww
        w[y==target]*=(len(d)/2)/w[y==target].sum()
    model=ExtraTreesClassifier(n_estimators=128,max_depth=13,min_samples_leaf=2,max_features=.7,n_jobs=4,random_state=seed);model.fit(a,y,sample_weight=w)
    return dict(model=model,columns=cols,median=med,training_cows=sorted(d.cow_group.unique()),excluded_cows=list(excluded),training_event_ids=sorted(d[d.target==1].parent_event_id.unique()),
        training_positive_events=int(d[d.target==1].parent_event_id.nunique()),training_negative_candidates=int((d.target==0).sum()),algorithm='observed-bin relative posture and persistence + event/cow weighted ExtraTrees',
        threshold=.7,threshold_status='not_yet_calibrated',raw_normalization='native counts; no automatic accelerometer offset repair',source='dataset/training_candidates.csv',
        training_data_quality='highest-confidence84-source only; abnormal nominal-calibration sources excluded',score_is_calibrated_probability=False)
