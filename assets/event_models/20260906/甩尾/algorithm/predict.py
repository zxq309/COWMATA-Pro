"""Run from a complete original JSON. No annotation or previous cache inputs."""
import os
os.environ.setdefault('OMP_NUM_THREADS','1');os.environ.setdefault('MKL_NUM_THREADS','1')
from pathlib import Path
from functools import lru_cache
import argparse,hashlib,json,pickle,time
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from .decoder import load_v2_json
from .quality import audit_session
from .features import extract,model_matrix,COLUMNS,VERSION

PACK=Path(__file__).resolve().parents[1]

@lru_cache(maxsize=12)
def load_model(path):
    # Load only the locally trained, trusted model bundles shipped in this package.
    with open(path,'rb') as f:bundle=pickle.load(f)
    if bundle['feature_version']!=VERSION or bundle['columns']!=COLUMNS:raise ValueError('Incompatible feature schema')
    return bundle

def select_model(package_root,cow):
    return package_root/'models/model.pkl','full_fit_deployment'

def select_candidates(frame,limit=12,separation=10):
    chosen=[]
    for i,r in frame.sort_values(['score','center_s'],ascending=[False,True]).iterrows():
        if all(abs(r.center_s-frame.loc[j,'center_s'])>=separation for j in chosen):chosen.append(i)
        if len(chosen)>=limit:break
    return frame.loc[chosen].sort_values('center_s').copy()

def approximate_wall(created,seconds):
    ms=round((created+seconds*1000)/5000)*5000
    return pd.to_datetime(ms,unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S')

def run_json(path,cow='',key='',device_hint='',package_root=PACK):
    start=time.perf_counter();path=Path(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    session=load_v2_json(path)
    quality,t,bad=audit_session(session,device_hint)
    modelpath,mode=select_model(Path(package_root),cow)
    quality.update(raw_path=str(path.resolve()),raw_sha256=digest,session_key=key,cow_group=cow,
        input_bytes=path.stat().st_size,model_mode=mode,model_file=modelpath.relative_to(package_root).as_posix(),
        loaded_old_cache=False,full_json_decoded=True,algorithm_version=VERSION)
    empty=pd.DataFrame(columns=['session_key','cow_group','center_s','end_s','score','approx_time_bj','message'])
    if not quality['core_eligible']:
        quality.update(valid_windows=0,clean_proposals=0,candidates=0,runtime_s=time.perf_counter()-start)
        return quality,empty,empty,np.array([])
    bundle=load_model(str(modelpath))
    with threadpool_limits(limits=1):
        w,features=extract(t,session.raw_values,bad,key,cow)
        if len(features):scores=bundle['model'].predict_proba(model_matrix(features))[:,1]
        else:scores=np.array([])
    frame=w.loc[features.index,['session_key','cow_group','center_s','end_s']].copy() if len(w) else empty.copy()
    frame['score']=scores
    candidates=select_candidates(frame) if len(frame) else empty.copy()
    candidates['approx_time_bj']=[approximate_wall(session.create_time_ms,x) for x in candidates.center_s]
    candidates['message']='附近可能发生甩尾'
    candidates['model_mode']=mode;candidates['score_is_probability']=False
    quality.update(valid_windows=len(w),clean_proposals=len(features),candidates=len(candidates),runtime_s=time.perf_counter()-start,
        training_cows=';'.join(bundle['training_cows']))
    return quality,candidates,frame,w.center_s.to_numpy() if len(w) else np.array([])

def main():
    p=argparse.ArgumentParser();p.add_argument('--json',type=Path,required=True);p.add_argument('--cow-id',default='')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--device-hint',default='');p.add_argument('--package-root',type=Path,default=PACK)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    q,c,_,_=run_json(a.json,a.cow_id,a.json.stem,a.device_hint,a.package_root)
    c.to_csv(a.output/'candidates.csv',index=False,encoding='utf-8-sig')
    (a.output/'quality.json').write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=q['core_eligible'],candidates=len(c),reason=q['reject_reason'],model=q['model_mode']),ensure_ascii=False))

if __name__=='__main__':main()
