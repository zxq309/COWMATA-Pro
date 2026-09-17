"""Train frozen HGB recipe using only the standalone dataset (no old caches)."""
import os
os.environ.setdefault('OMP_NUM_THREADS','4');os.environ.setdefault('MKL_NUM_THREADS','4')
from pathlib import Path
import argparse,hashlib,json,pickle
import numpy as np
import pandas as pd
from sklearn.experimental import enable_hist_gradient_boosting
from sklearn.ensemble import HistGradientBoostingClassifier
from threadpoolctl import threadpool_limits
from .features import COLUMNS,VERSION

PACK=Path(__file__).resolve().parents[1]
PARAMETERS=dict(max_iter=160,max_leaf_nodes=7,max_depth=3,min_samples_leaf=5,
    learning_rate=.06,l2_regularization=10,early_stopping=False,random_state=20260906)

def train_one(x,meta,held_out):
    eligible=meta[meta.cow_group!=held_out] if held_out else meta
    positive=[]
    for _,g in eligible[eligible.label=='known_positive'].groupby('event_id'):
        # arcsinh is monotonic; the final column gyro_rms is the same ranking as before.
        rank=x[g.index,COLUMNS.index('gyro_rms')]
        positive.extend(g.assign(gyro_rms=rank).sort_values('gyro_rms',ascending=False).head(6).index)
    negative=eligible.index[eligible.label=='proxy_negative'].to_numpy();positive=np.array(positive,int)
    if not len(positive) or not len(negative):raise ValueError('Both positive and proxy-negative training data are required')
    ids=np.r_[negative,positive];y=np.r_[np.zeros(len(negative),int),np.ones(len(positive),int)]
    nc=meta.loc[negative,'cow_group'].value_counts();pc=meta.loc[positive,'event_id'].value_counts()
    weights=np.r_[[len(negative)/len(nc)/nc[meta.loc[j,'cow_group']] for j in negative],
        [.1*len(negative)/len(pc)/pc[meta.loc[j,'event_id']] for j in positive]]
    weights*=len(ids)/weights.sum()
    with threadpool_limits(limits=4):
        model=HistGradientBoostingClassifier(**PARAMETERS).fit(x[ids],y,sample_weight=weights)
    return dict(model=model,columns=COLUMNS,feature_version=VERSION,held_out_cow=held_out,
        training_cows=sorted(meta.loc[ids,'cow_group'].unique()),training_event_ids=sorted(pc.index),
        positive_window_ids=meta.loc[positive,'window_id'].tolist(),negative_window_ids=meta.loc[negative,'window_id'].tolist(),
        all_clean_proxy_negatives=True,parameters=PARAMETERS,per_file_candidates=12,min_separation_s=10,
        score_is_probability=False,mode='offline_session_ranking',deployment_accuracy_validated=False)

