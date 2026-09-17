"""Refit pure91 with the frozen trigger selected in the original cow-disjoint validation."""
from pathlib import Path
import argparse, json, pickle
import numpy as np
import pandas as pd
from sklearn.experimental import enable_hist_gradient_boosting
from sklearn.ensemble import HistGradientBoostingClassifier
from threadpoolctl import threadpool_limits
def fit(x,w):
    mask=w.fit_eligible.values;f=w.loc[mask];xx=x[mask];y=f.train_target.values
    weights=np.ones(len(y));n=(y==0).sum();assert n>0 and y.sum()>0
    _,inv,cnt=np.unique(f.positive_event_id.values[y==1],return_inverse=True,return_counts=True)
    weights[y==1]=1/cnt[inv];weights[y==1]*=n/weights[y==1].sum()
    _,inv,cnt=np.unique(f.cow_group.values[y==0],return_inverse=True,return_counts=True)
    weights[y==0]=1/cnt[inv];weights[y==0]*=n/weights[y==0].sum()
    model=HistGradientBoostingClassifier(max_iter=100,max_leaf_nodes=15,learning_rate=.08,l2_regularization=5,early_stopping=False,random_state=20260906)
    with threadpool_limits(limits=4):model.fit(xx,y,sample_weight=weights)
    return model

def main():
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, default=root.parents[1] / '02_数据集/排尿')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    w = pd.read_pickle(a.dataset / 'training/windows.pkl')
    cols = json.loads((root / 'feature_columns.json').read_text(encoding='utf-8'))
    x = w[cols].values.astype(np.float32)
    config = json.loads((root / 'training_config.json').read_text(encoding='utf-8'))
    config.pop('elapsed_seconds', None)
    config.update(training_cows=sorted(w.loc[w.fit_eligible, 'cow_group'].unique()),
                  training_event_ids=sorted(w.loc[w.train_target.eq(1), 'positive_event_id'].unique()))
    pack = dict(config, model=fit(x, w), feature_columns=cols)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('wb') as f:
        pickle.dump(pack, f)
    print('Saved', a.output, 'events', len(pack['training_event_ids']))

if __name__ == '__main__':
    main()
