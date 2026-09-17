"""Event-balanced bagged positive-unlabeled ranking; U is not verified negative."""
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

def fit_local(X,meta,cows,seed,family):
    if family != 'local_extra':raise ValueError('Only the final ExtraTrees recipe is included')
    rng=np.random.RandomState(seed);models=[];records=[]
    for rep in range(3):
        indices=[];targets=[];weights=[]
        for cow in cows:
            p=meta[meta.cow_group.eq(cow)&meta.local_positive];u=meta[meta.cow_group.eq(cow)&meta.unlabeled_pool&meta.strict_candidate]
            cluster=u.session_key+'_'+(u.spot_s//5).astype(int).astype(str)
            sample=u.assign(cluster=cluster,draw=rng.rand(len(u))).sort_values('draw').drop_duplicates('cluster')
            ui=sample.iloc[rng.choice(len(sample),min(1600,len(sample)),replace=False)].index.values
            freq=p.local_bag.value_counts();pw=1/p.local_bag.map(freq).values
            indices.extend(p.index);targets.extend([1]*len(p));weights.extend(.5*pw/pw.sum())
            nw=np.ones(len(ui))
            indices.extend(ui);targets.extend([0]*len(ui));weights.extend(.5*nw/nw.sum())
            records.append(dict(member=rep,cow_group=cow,positive_bags=p.local_bag.nunique(),positive_instances=len(p),unlabeled_reference=len(ui),state_supported_negative=int(meta.loc[ui,'explicit_state_negative'].sum())))
        weights=np.asarray(weights);weights*=len(weights)/weights.sum()
        estimator=make_pipeline(SimpleImputer(strategy='median',add_indicator=True),ExtraTreesClassifier(n_estimators=160,max_features=.65,min_samples_leaf=2,n_jobs=4,random_state=seed+rep))
        with threadpool_limits(limits=4):estimator.fit(X.iloc[indices].values,targets,extratreesclassifier__sample_weight=weights)
        models.append(estimator)
    return dict(models=models,features=list(X),training_cows=cows,training_records=records,stage=family,
                feature_version='clean_local_point132_20260906_v2',calibration=None,quality_version='clean_local_motif_20260906_v2',
                training_positive_event_ids=sorted(meta[meta.cow_group.isin(cows)&meta.local_positive].local_bag.unique()),
                learning_semantics='clean_positive_versus_clean_unlabeled_ranking_not_probability')

