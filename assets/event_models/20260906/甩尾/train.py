"""Refit the final tail-wagging HGB from the curated dataset."""
from pathlib import Path
import argparse, hashlib, pickle
import numpy as np
import pandas as pd
from algorithm.train import train_one
from algorithm.features import COLUMNS

def main():
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, default=root.parents[1] / '02_数据集/甩尾')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    meta = pd.read_csv(a.dataset / 'metadata/sample_manifest.csv', dtype={'cow_group': str}).reset_index(drop=True)
    source = a.dataset / 'training_features.npz'
    with np.load(source, allow_pickle=False) as z:
        assert z['window_id'].tolist() == meta.window_id.tolist()
        assert z['columns'].tolist() == COLUMNS
        bundle = train_one(z['x'], meta, '')
    bundle['dataset_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('wb') as f:
        pickle.dump(bundle, f)
    print('Saved', a.output, 'events', len(bundle['training_event_ids']))

if __name__ == '__main__':
    main()
