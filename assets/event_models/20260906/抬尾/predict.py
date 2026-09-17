# -*- coding: utf-8 -*-
"""Run the actual algorithm on complete JSON files, without labels or old caches."""
import argparse,json,time,sys,hashlib
from pathlib import Path
import pandas as pd
from tailspot.signal import read_json_features
from tailspot.model import load_model,infer

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--json',type=Path,required=True,help='One complete JSON or directory of JSONs')
    ap.add_argument('--model',type=Path,default=Path(__file__).parent/'models/model.pkl');ap.add_argument('--cow',default='unknown')
    ap.add_argument('--budget',type=int,choices=[12,24,48,96,192],default=96);ap.add_argument('--scope',choices=['reference','observed'],default='reference')
    ap.add_argument('--expected-device',help='Optional known device ID to check the cow/device binding')
    ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();files=sorted(args.json.rglob('*.json')) if args.json.is_dir() else [args.json]
    if not files:ap.error('No JSON files found')
    if args.json.is_dir():
        try:args.output.resolve().relative_to(args.json.resolve())
        except ValueError:pass
        else:ap.error('Output must be outside the input directory, to keep subsequent scans free of result JSONs')
    model=load_model(args.model);rows=[];audit=[];errors=[];args.output.mkdir(parents=True,exist_ok=True)
    for i,path in enumerate(files):
        start=time.perf_counter()
        try:
            rec=read_json_features(path,cow=args.cow,expected_device=args.expected_device);alerts,peaks,score=infer(rec,model,args.budget,args.scope);alerts['source_json']=str(path.resolve());rows.append(alerts)
            qc=rec['quality'];qc.update(alerts=len(alerts),elapsed_s=time.perf_counter()-start);audit.append(qc)
        except Exception as ex:errors.append(dict(source_json=str(path.resolve()),error=str(ex)))
        if (i+1)%25==0:print('Scanned',i+1,'/',len(files),flush=True)
    if rows:pd.concat(rows,ignore_index=True).to_csv(args.output/'event_hints.csv',index=False,encoding='utf-8-sig')
    else:pd.DataFrame(columns=['candidate_id','event','hint','status']).to_csv(args.output/'event_hints.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(audit).to_csv(args.output/'file_quality.csv',index=False,encoding='utf-8-sig');pd.DataFrame(errors,columns=['source_json','error']).to_csv(args.output/'errors.csv',index=False,encoding='utf-8-sig')
    summary=dict(files_requested=len(files),files_scanned=len(audit),files_failed=len(errors),alerts=sum(len(r) for r in rows),scope=args.scope,budget_reference=args.budget,
        labels_loaded=False,old_cache_used=False,source='complete JSON read and fresh features',scores_are_probabilities=False,offline=True,
        model_path=str(args.model.resolve()),model_sha256=hashlib.sha256(args.model.read_bytes()).hexdigest(),
        threshold=float(model['training_cv_thresholds'][str(args.budget)]),budget_is_training_target_not_test_cap=True)
    (args.output/'run_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False),flush=True)
    if errors:raise SystemExit(2)
if __name__=='__main__':sys.stdout.reconfigure(encoding='utf-8');main()
