# -*- coding: utf-8 -*-
"""Train the fixed algorithm from the standalone clean dataset."""
import argparse,json,sys
from pathlib import Path
from tailspot.training import load_dataset,fit,calibrate,save_model

def main():
    root=Path(__file__).resolve().parent;p=argparse.ArgumentParser()
    p.add_argument('--dataset',type=Path,default=root.parents[1]/'02_数据集/抬尾');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();x,m=load_dataset(args.dataset);ids=m.index.values
    thresholds,audit=calibrate(x,m,ids,20260926);model=fit(x,m,ids,20260999)
    model.update(training_cv_thresholds=thresholds,threshold_calibration=audit)
    save_model(model,args.output);args.output.with_suffix('.json').write_text(json.dumps(dict(
        training_cows=model['training_cows'],events=model['independent_positive_events'],thresholds=thresholds,
        calibration=audit,fit_replay_is_not_independent_validation=True),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved',args.output,'independent positive events',len(model['independent_positive_events']),flush=True)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
