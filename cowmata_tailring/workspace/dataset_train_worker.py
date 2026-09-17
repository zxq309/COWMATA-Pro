"""Fit native recipes on the exported TRAIN fold only; output is a new model."""
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    root,title,pack = Path(request['dataset']),request['head'],Path(request['pack'])
    folder = root/'事件识别'/title
    behavior=root/'行为数据集'/({'起立':'起立过程','卧倒':'卧倒过程'}.get(title,title))
    if behavior.is_dir():
        folder=behavior
        if not (folder/'readiness.json').is_file():
            raise ValueError('请先在行为构建中勾选生成已接入算法的训练特征。')
    readiness = json.loads((folder/'readiness.json').read_text(encoding='utf-8'))
    if not readiness['ready_to_fit']:
        raise ValueError(readiness['reason'])
    output = Path(request['output'])
    if output.exists():
        raise ValueError('Model output already exists')
    sys.path.insert(0,str(pack/title))
    if title == '起立':
        from algorithm.train import fit
        table = pd.read_csv(folder/'training_candidates.csv',dtype={'cow_group':str}).fillna({'parent_event_id':''})
        bundle = fit(table,[],20260906)
        bundle['training_data_quality'] = 'current_mother_labels_quality_screened'
    elif title == '卧倒':
        from learner import fit_local
        z = np.load(folder/'training/features.npz',allow_pickle=False)
        x = pd.DataFrame(z['X'],columns=z['columns'])
        meta = pd.read_csv(folder/'training/candidates.csv',dtype={'cow_group':str}).fillna('')
        assert list(z['candidate_id'])==list(meta.candidate_id)
        bundle = fit_local(x,meta,sorted(meta.cow_group.unique()),20260906,'local_extra')
        bundle.update(threshold=None,threshold_status='requires_cow_disjoint_validation')
    elif title == '排尿':
        from train import fit
        meta = pd.read_pickle(folder/'training/windows.pkl')
        columns = json.loads((pack/title/'feature_columns.json').read_text(encoding='utf-8'))
        bundle = dict(model=fit(meta[columns].to_numpy(np.float32),meta),feature_columns=columns,
                      threshold_status='requires_cow_disjoint_validation')
    elif title == '抬尾':
        from tailspot.training import fit, load_dataset
        x,meta = load_dataset(folder)
        bundle = fit(x,meta,np.arange(len(meta)),20260906)
        bundle['threshold_status'] = 'requires_cow_disjoint_validation'
    elif title == '甩尾':
        from algorithm.train import train_one
        z = np.load(folder/'training_features.npz',allow_pickle=False)
        meta = pd.read_csv(folder/'metadata/sample_manifest.csv',dtype={'cow_group':str}).fillna('')
        assert list(z['window_id'])==list(meta.window_id)
        bundle = train_one(z['x'],meta,'')
    else:
        raise ValueError('Unknown event head')
    bundle.update(dataset=str(root),training_cows=readiness['training_cows'],
                  validation_and_test_used=False,automatic_deployment=False,
                  deployment_status='research_model_requires_validation',score_is_calibrated_probability=False)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream:
        pickle.dump(bundle,stream,protocol=4)
    print(json.dumps(dict(head=title,training_cows=readiness['training_cows'],output=str(output)),ensure_ascii=True))


if __name__ == '__main__':
    main()
