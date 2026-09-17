# -*- coding: utf-8 -*-
"""Reproducible cow-disjoint PU bagging. Reads only the standalone dataset."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
from sklearn.model_selection import GroupKFold
from threadpoolctl import threadpool_limits
from .model import predict,nms

BUDGETS=(12,24,48,96,192)

def load_dataset(folder):
    folder=Path(folder)
    x=pd.read_pickle(folder/'training/features.pkl');m=pd.read_pickle(folder/'training/points.pkl')
    assert x.index.equals(m.index) and m.reference_clean.all()
    assert set(m.role)<=set(['P','U','IGNORE'])
    assert m.loc[m.role=='P'].groupby('positive_event_id').event_weight.sum().round(8).eq(1).all()
    return x,m

def fit(x,m,train,seed):
    train=np.asarray(train);tm=m.loc[train];tc=sorted(tm.cow_group.unique())
    pids=tm.index[tm.role=='P'].values;pool=tm.index[tm.role=='U'].values
    if not len(pids) or not len(pool):raise ValueError('Need positive events and unlabelled reference data')
    pw=m.loc[pids,'event_weight'].values.astype(float);rng=np.random.RandomState(seed)
    reference=rng.choice(pool,min(12000,len(pool)),replace=False);columns=list(x)
    imputer=SimpleImputer(strategy='median').fit(x.loc[np.r_[reference,pids],columns])
    models=[];bags=[]
    for j in range(4):
        u=[]
        for cow in tc:
            g=pool[m.loc[pool,'cow_group'].values==cow];u.extend(rng.choice(g,min(768,len(g)),replace=False))
        u=np.asarray(u,dtype=int);ids=np.r_[pids,u];y=np.r_[np.ones(len(pids)),np.zeros(len(u))]
        # Class 0 is a sampled unlabelled proxy; it is never evaluation ground truth.
        weights=np.r_[pw/pw.sum(),np.ones(len(u))/len(u)]*len(ids)/2
        tree=ExtraTreesClassifier(n_estimators=64,max_depth=10,min_samples_leaf=2,max_features=.7,n_jobs=4,random_state=seed+j)
        with threadpool_limits(limits=4):tree.fit(imputer.transform(x.loc[ids,columns]),y,sample_weight=weights)
        models.append(tree);bags.append(dict(bag=j,positive_points=len(pids),unlabeled_points=len(u),unlabeled_cows=tc))
    return dict(kind='clean_legacy',features=columns,imputer=imputer,models=models,bags=bags,
        training_cows=tc,positive_ids=pids.tolist(),independent_positive_events=sorted(m.loc[pids,'positive_event_id'].unique()),
        random_seed=seed,quality_policy='strict_reference_21s',offline=True,context_future_max_s=25.5,
        score_is_probability=False,unlabeled_is_verified_negative=False,version='tailspot_standalone_v1')

def calibrate(x,m,train,seed):
    """Thresholds from inner cow-disjoint scores, with NO outer-cow data/labels."""
    train=np.asarray(train);cows=m.loc[train,'cow_group'].values;scores=pd.Series(np.nan,index=train);splits=[]
    for j,(ti,vi) in enumerate(GroupKFold(3).split(train,groups=cows)):
        tr,va=train[ti],train[vi];model=fit(x,m,tr,seed+j*10)
        vc=sorted(m.loc[va,'cow_group'].unique());assert not set(vc)&set(model['training_cows'])
        scores.loc[va]=predict(model,x.loc[va])
        splits.append(dict(inner_fold=j,training_cows=model['training_cows'],validation_cows=vc,
            positive_events=model['independent_positive_events'],seed=seed+j*10))
    assert scores.notna().all()
    peaks=nms(m.loc[train].reset_index(drop=True),scores.loc[train].values,'reference').sort_values('score',ascending=False,kind='stable')
    hours=len(train)*2/3600;thresholds={}
    for budget in BUDGETS:
        k=min(len(peaks),int(np.ceil(hours*budget/24)))
        if not k:raise ValueError('No eligible calibration peaks')
        thresholds[str(budget)]=float(peaks.iloc[k-1].score)
    return thresholds,dict(training_cows=sorted(set(cows)),training_reference_hours=hours,inner_splits=splits,
        threshold_method='pooled inner-cow OOF peak quantile',budget_is_training_target_not_test_cap=True)

def save_model(model,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('wb') as f:pickle.dump(model,f,pickle.HIGHEST_PROTOCOL)
