"""Train solely from this package's clean P/U feature bank, with fixed cow folds."""
from pathlib import Path
import argparse, json, sys, hashlib, time
import numpy as np
import pandas as pd
import joblib
from learner import fit_local

sys.stdout.reconfigure(encoding='utf-8')
BASE = Path(__file__).resolve().parent

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=BASE.parents[1]/'02_数据集/卧倒')
    p.add_argument('--protocol',type=Path,default=BASE/'training_config.json')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
    z=np.load(a.dataset/'training/features.npz',allow_pickle=False)
    X=pd.DataFrame(z['X'],columns=z['columns'])
    m=pd.read_csv(a.dataset/'training/candidates.csv',dtype={'cow_group':str}).fillna('')
    assert list(m.candidate_id)==list(z['candidate_id'])
    assert m.strict_candidate.all() and not (m.local_positive & m.unlabeled_pool).any()
    assert np.isfinite(X.values[~np.isnan(X.values)]).all()
    protocol=json.loads(a.protocol.read_text(encoding='utf-8'))
    assert set(m.cow_group)==set(protocol['cows'])
    q=pd.read_csv(a.dataset/'quality/source_session_audit.csv',dtype={'cow_group':str})
    jobs=[dict(name='model',family=protocol['full_family'],seed=protocol['full_training_seed'], training_cows=protocol['cows'],threshold=protocol['model_spec']['threshold'],test_cow=None)]
    records=[]
    for j in jobs:
        start=time.perf_counter()
        assert j['test_cow'] not in j['training_cows']
        b=fit_local(X,m,j['training_cows'],j['seed'],j['family'])
        b.update(threshold=j['threshold'],point_nms_seconds=20,
                 unsupported_devices=protocol['model_spec']['unsupported_devices'],
                 known_training_devices=sorted(q[q.cow_group.isin(j['training_cows'])].device.unique()),
                 clean_training_groups=len(b['training_positive_event_ids']),hardware_quiet_norm_range=[.9,1.1],minimum_quiet_bins=100,
                 seed=j['seed'],test_cow=j['test_cow'],quality_version='grade_A_local77_v3_20260906',
                 use='deployment prototype; training cows included' if j['test_cow'] is None else 'cow-disjoint raw JSON replay',
                 training_data_sha256=hashlib.sha256((a.dataset/'training/features.npz').read_bytes()).hexdigest())
        path=a.output;joblib.dump(b,path)
        rec=dict(model=path.name,family=j['family'],training_cows=j['training_cows'],test_cow=j['test_cow'],
                 positive_groups=b['clean_training_groups'],seed=j['seed'],threshold=j['threshold'],
                 sha256=hashlib.sha256(path.read_bytes()).hexdigest(),training_seconds=time.perf_counter()-start)
        records.append(rec);print(json.dumps(rec,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
