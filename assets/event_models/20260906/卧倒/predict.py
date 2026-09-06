# -*- coding: utf-8 -*-
"""Read-only offline event spotting trained on grade A clean local motifs."""
from pathlib import Path
import argparse,sys,json,hashlib
import numpy as np,pandas as pd,joblib
from threadpoolctl import threadpool_limits
from source_v2_io import load_v2_json
from signal_features import bin5,runs
from predict_events import extract,valid_seconds,output_rows
from core import nms
sys.stdout.reconfigure(encoding='utf-8')

def inspect_signal(session):
    t=session.elapsed_ms-session.elapsed_ms[0];raw=session.raw_values;good=valid_seconds(t);z=bin5(t,raw,good)
    bins=(t//200).astype(int);nb=len(z['t']);cnt=np.bincount(bins,minlength=nb)
    mu=np.stack([np.bincount(bins,weights=raw[:,j].astype(float),minlength=nb)/np.maximum(cnt,1) for j in range(3)],axis=1)/4096
    quiet=(z['gyro']<5)&(z['dyn']<.015)&z['valid'];norm=float(np.median(np.linalg.norm(mu[quiet],axis=1))) if quiet.sum()>=100 else None
    clipped=np.any(np.abs(raw[:,:6].astype(np.int32))>=32760,axis=1);zero=np.all(raw[:,:6]==0,axis=1);frozen=np.zeros(len(t),bool)
    for a,b in runs(np.all(raw[1:]==raw[:-1],axis=1)):
        if t[b]-t[a]>=1000:frozen[a:b+1]=True
    bad=np.bincount(bins,weights=(clipped|zero|frozen).astype(int),minlength=nb)>0
    return dict(quiet_norm_g=norm,quiet_bins=int(quiet.sum()),clipped_frames=int(clipped.sum()),zero_frames=int(zero.sum()),frozen_frames=int(frozen.sum())),bad

def run(session,bundle,threshold=None):
    info,bad=inspect_signal(session);device=session.device.upper();nominal=info['quiet_norm_g'] is not None and .9<=info['quiet_norm_g']<=1.1
    info['source_integrity_supported']=session.diagnostics.phase_score>=.95 and session.diagnostics.dropped_trailing_bytes==0
    info.update(device=device,hardware_supported=device not in bundle['unsupported_devices'] and nominal,known_training_device=device in bundle['known_training_devices'])
    if not info['source_integrity_supported']:
        info['status']='NEEDS_SOURCE_REVIEW';return pd.DataFrame(columns=['session_key','cow_group','spot_s','score','candidate_id','segment_start_s','support_grade']),pd.DataFrame(),info
    if not info['hardware_supported']:
        info['status']='NEEDS_HARDWARE_REVIEW';return pd.DataFrame(columns=['session_key','cow_group','spot_s','score','candidate_id','segment_start_s','support_grade']),pd.DataFrame(),info
    X,m,good=extract(session,bundle);info['scanned_valid_seconds']=int(good.sum())
    if not len(m):info['status']='NO_SIGNAL_CANDIDATES';return m,m,info
    rawclean=[]
    for _,r in m.iterrows():
        l=max(int(round(r.segment_start_s*5)),int(round(r.start_s*5))-40);rr=min(int(round(r.segment_end_s*5)),int(round(r.end_s*5))+40)
        rawclean.append(not bool(bad[l:rr].any()))
    rawclean=np.array(rawclean);score=np.full(len(m),-1.)
    with threadpool_limits(limits=4):score[rawclean]=np.mean([model.predict_proba(X.loc[rawclean].values)[:,1] for model in bundle['models']],axis=0) if rawclean.any() else []
    if not rawclean.any():info['status']='NO_CLEAN_SIGNAL_CANDIDATES';return pd.DataFrame(),m,info
    ranked=nms(m[rawclean],score,20);th=bundle['threshold'] if threshold is None else threshold;ranked=ranked[ranked.score>=th].copy()
    context=m.set_index('candidate_id');ranked['support_grade']=['clean_local_8s' if min(context.loc[r.candidate_id,'spot_s']-context.loc[r.candidate_id,'segment_start_s'],context.loc[r.candidate_id,'segment_end_s']-context.loc[r.candidate_id,'spot_s'])>=4 else 'short_context_review_only' for _,r in ranked.iterrows()]
    m['score']=score;info.update(status='SCANNED',signal_candidates=len(m),raw_quality_rejected=int((~rawclean).sum()),threshold=float(th));return ranked,m,info

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model',type=Path,default=Path(__file__).resolve().parent/'models/model.joblib');p.add_argument('--threshold',type=float)
    a=p.parse_args()
    if a.threshold is not None and not 0<=a.threshold<=1:p.error('threshold must be in [0,1]')
    bundle=joblib.load(a.model);session=load_v2_json(a.input);alerts,meta,info=run(session,bundle,a.threshold)
    output=output_rows(alerts,session) if len(alerts) else pd.DataFrame(columns=['code','around_s','around_wall_bj','ranking_score','device','message'])
    if len(alerts):
        # output_rows sorts by displayed time; join grades through precise rounded seconds.
        grades={int(np.round(r.spot_s/5)*5):r.support_grade for _,r in alerts.iterrows()};output['support_grade']=output.around_s.map(grades)
    else:output['support_grade']=pd.Series(dtype=str)
    a.output.parent.mkdir(parents=True,exist_ok=True);output.to_csv(a.output,index=False,encoding='utf-8-sig')
    info.update(input=str(a.input.resolve()),model=str(a.model.resolve()),alerts=len(output),training_groups=bundle['clean_training_groups'],
        mode='OFFLINE_FULL_RECORDING',output_semantics='Approximate occurrence; PU score is not probability. Short context is review-only, not clean-training eligible.',
        unsupported_result='A hardware rejection is unknown, not a zero-event or negative recording.',
        raw_sha256=hashlib.sha256(a.input.read_bytes()).hexdigest())
    a.output.with_suffix('.metadata.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(info,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
