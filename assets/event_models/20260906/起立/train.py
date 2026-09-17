"""Refit the curated stand-up model with the frozen release threshold."""
from pathlib import Path
import argparse, json, joblib
import pandas as pd
from algorithm.train import fit, SEED

def main():
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, default=root.parents[1] / '02_数据集/起立')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    settings = json.loads((root / 'training_config.json').read_text(encoding='utf-8'))
    table = pd.read_csv(a.dataset / 'training_candidates.csv', dtype={'cow_group': str, 'parent_event_id': str})
    table.parent_event_id = table.parent_event_id.fillna('')
    bundle = fit(table, [], SEED + 999)
    bundle.update(settings)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, a.output)
    print('Saved', a.output, 'events', bundle['training_positive_events'])

if __name__ == '__main__':
    main()
