"""Native 20260906 feature/consumer adapters; unknown is never ground truth.

The worker executes one head per isolated process in the reviewed model runtime.
Feature extraction never reads labels. Labels are joined only afterwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CODES = {'起立':'STANDING_UP','卧倒':'LYING_DOWN','排尿':'URINATION',
         '抬尾':'TAIL_RAISED','甩尾':'TAIL_WAGGING'}


def candidate_role(second, events, code):
    hits = []
    for event in events:
        if event['code'] != code and code not in event.get('negative_for',[]):
            continue
        start, end = event['start_ms']/1000, event.get('end_ms')
        covers = abs(second-start) <= 1 if end is None else start <= second <= end/1000
        if not covers:
            continue
        if not event['training_eligible']:
            return 'IGNORE',''
        hits.append(('N' if event['role']=='negative' else 'P',event['event_id']))
    if len({role for role,_ in hits}) > 1 or len({eid for _,eid in hits}) > 1:
        return 'IGNORE',''
    return hits[0] if hits else ('U','')


def save_consumer(root, title, x, meta, audit):
    import numpy as np
    import pandas as pd
    root = Path(root)
    root.mkdir(parents=True,exist_ok=True)
    (root/'training').mkdir(exist_ok=True)
    (root/'quality').mkdir(exist_ok=True)
    meta = meta.reset_index(drop=True)
    x = x.reset_index(drop=True)
    assert len(x) == len(meta)
    meta.to_csv(root/'candidate_manifest.csv',index=False,encoding='utf-8-sig')
    np.savez_compressed(root/'candidate_features.npz',X=x.to_numpy(np.float32),
                        columns=np.asarray(list(x),str),candidate_id=meta.candidate_id.to_numpy(str))
    # Unknown references are permitted only for explicit PU training recipes.
    roles = ['P','U','N'] if title in {'卧倒','抬尾'} else ['P','N','PROXY']
    mask = meta.split.eq('train') & meta.role.isin(roles)
    if title == '卧倒':
        positive_cows = set(meta.loc[mask & meta.role.eq('P'),'cow_group'])
        mask &= meta.cow_group.isin(positive_cows)
    m = meta.loc[mask].reset_index(drop=True).copy()
    f = x.loc[mask].reset_index(drop=True).copy()
    for split in ('validation','test'):
        selected = meta.split.eq(split)
        directory = root/split
        directory.mkdir(exist_ok=True)
        meta.loc[selected].to_csv(directory/'candidates.csv',index=False,encoding='utf-8-sig')
        np.savez_compressed(directory/'features.npz',X=x.loc[selected].to_numpy(np.float32),
                            columns=np.asarray(list(x),str),candidate_id=meta.loc[selected,'candidate_id'].to_numpy(str))
    if title == '起立':
        out = m[['candidate_id','session_key','cow_group','spot_s']].copy()
        out['parent_event_id'],out['target'] = m.event_id,m.role.eq('P').astype(int)
        pd.concat([out,f],axis=1).to_csv(root/'training_candidates.csv',index=False,encoding='utf-8-sig')
    elif title == '卧倒':
        m['strict_candidate'] = True
        m['local_positive'],m['unlabeled_pool'] = m.role.eq('P'),~m.role.eq('P')
        m['local_bag'],m['explicit_state_negative'] = m.event_id,m.role.eq('N')
        m.to_csv(root/'training/candidates.csv',index=False,encoding='utf-8-sig')
        np.savez_compressed(root/'training/features.npz',X=f.to_numpy(np.float32),
                            columns=np.asarray(list(f),str),candidate_id=m.candidate_id.to_numpy(str))
    elif title == '排尿':
        m['fit_eligible'],m['train_target'],m['positive_event_id'] = True,m.role.eq('P').astype(int),m.event_id
        pd.concat([m,f],axis=1).to_pickle(root/'training/windows.pkl',protocol=4)
    elif title == '抬尾':
        m['reference_clean'],m['positive_event_id'] = True,m.event_id
        m['role'] = m.role.replace({'N':'U'})
        sizes = m.loc[m.role.eq('P')].groupby('event_id').size()
        m['event_weight'] = m.event_id.map(1/sizes).fillna(0)
        m.to_pickle(root/'training/points.pkl',protocol=4)
        f.to_pickle(root/'training/features.pkl',protocol=4)
    elif title == '甩尾':
        m['window_id'],m['label'] = m.candidate_id,np.where(m.role.eq('P'),'known_positive','proxy_negative')
        # The legacy consumer calls its class zero proxy_negative; keep the stronger source role too.
        (root/'metadata').mkdir(exist_ok=True)
        m.to_csv(root/'metadata/sample_manifest.csv',index=False,encoding='utf-8-sig')
        np.savez_compressed(root/'training_features.npz',x=f.to_numpy(np.float32),
                            columns=np.asarray(list(f),str),window_id=m.window_id.to_numpy(str))
    if audit or not (root/'quality/source_session_audit.csv').exists():
        pd.DataFrame(audit).to_csv(root/'quality/source_session_audit.csv',index=False,encoding='utf-8-sig')
    positive = m.role.eq('P')
    reference = ~positive
    enough = bool(positive.any() and reference.any())
    if title == '卧倒':
        enough = enough and all(g.role.eq('P').any() and (~g.role.eq('P')).any() for _,g in m.groupby('cow_group'))
    result = dict(head=title,candidates=len(meta),training_candidates=len(m),
        training_events=int(m.loc[positive,'event_id'].nunique()),training_cows=sorted(m.cow_group.unique()),
        unknown_candidates=int(meta.role.eq('U').sum()),ready_to_fit=enough,
        reason='' if enough else '缺少训练牛中的有效正事件或所需参考样本；不得用未知冒充人工负样本',
        unknown_is_verified_negative=False,split_policy='shared_cow_split',
        positive_join='candidate center inside interval; point tolerance 1 second',
        feature_recipe='reviewed_20260906',feature_count=len(x.columns))
    (root/'readiness.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def extract_native(title, path, key, cow, pack):
    import numpy as np
    import pandas as pd
    sys.path.insert(0,str(Path(pack)/title))
    if title == '起立':
        from algorithm.detector import extract, feature_columns
        from algorithm.raw_io import load_v2_json
        x,m,q = extract(load_v2_json(path),key,cow)
        x = x[feature_columns(x)]
        keep = np.full(len(m),not q['calibration_risk'])
    elif title == '卧倒':
        from predict import inspect_signal
        from predict_events import extract
        from source_v2_io import load_v2_json
        # Column names are plain JSON, not executable serialized models.
        columns = json.loads((Path(__file__).resolve().parents[2]/'assets/dataset_recipes/lying-down-columns.json').read_text(encoding='utf-8'))
        session = load_v2_json(path)
        x,m,good = extract(session,{'features':columns,'calibration':None},key,cow)
        q,bad = inspect_signal(session)
        keep = np.zeros(len(m),bool)
        nominal = q['quiet_norm_g'] is not None and .9 <= q['quiet_norm_g'] <= 1.1
        for i,r in m.iterrows():
            left=max(round(r.segment_start_s*5),round(r.start_s*5)-40)
            h=min(round(r.segment_end_s*5),round(r.end_s*5)+40)
            keep[i] = nominal and not bad[left:h].any() and min(r.spot_s-r.segment_start_s,r.segment_end_s-r.spot_s)>=4
        q.update(device=session.device,session_key=key,cow_group=cow)
    elif title == '排尿':
        from detector import prepare_json
        w,quality,q = prepare_json(path,cow,key)
        columns = json.loads((Path(pack)/title/'feature_columns.json').read_text(encoding='utf-8'))
        x = w.reindex(columns=columns)
        m = w.reindex(columns=['session_key','cow_group','end_s']).copy()
        m['spot_s'] = m.end_s-15
        m['candidate_id'] = [key+':'+str(i) for i in range(len(m))]
        keep = quality.signal_clean.to_numpy(bool) & q['strict_calibration_screen_pass']
    elif title == '抬尾':
        from tailspot.signal import read_json_features
        rec = read_json_features(path,cow,session_key=key)
        x,m,q = rec['features'],rec['meta'],rec['quality']
        keep = m.reference_clean.to_numpy(bool)
    elif title == '甩尾':
        from algorithm.decoder import load_v2_json
        from algorithm.features import COLUMNS, extract, model_matrix
        from algorithm.quality import audit_session
        session = load_v2_json(path)
        q,t,bad = audit_session(session,'')
        w,f = extract(t,session.raw_values,bad,key,cow)
        x = pd.DataFrame(model_matrix(f),columns=COLUMNS)
        m = w.loc[f.index,['session_key','cow_group','center_s','end_s']].reset_index(drop=True)
        m['spot_s'] = m.center_s
        m['candidate_id'] = [key+':'+str(i) for i in range(len(m))]
        keep = np.full(len(m),q['core_eligible'])
    else:
        raise ValueError('Unknown event head')
    q.update(session_key=key,cow_group=cow)
    for column in ('candidate_id','session_key','cow_group','spot_s'):
        if column not in m:
            m[column] = pd.Series(dtype=float if column=='spot_s' else str)
    return x.loc[keep].reset_index(drop=True),m.loc[keep].reset_index(drop=True),q


def main():
    import pandas as pd
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    root,title,pack = Path(request['dataset']),request['head'],request['pack']
    events = [json.loads(line) for line in (root/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    sources = [json.loads(line) for line in (root/'sources.jsonl').read_text(encoding='utf-8').splitlines()]
    splits = json.loads((root/'cow-splits.json').read_text(encoding='utf-8'))
    xs,ms,audit = [],[],[]
    for i,source in enumerate(sources):
        if request.get('cancel') and Path(request['cancel']).exists():
            raise InterruptedError('Dataset feature job cancelled')
        rows = [e for e in events if e['source_asset_id']==source['asset_id'] and e['coordinates']=='parent_imu_ms']
        cows = {e['cow_id'] for e in rows if e['training_eligible']}
        if len(cows)!=1 or any(e['exclusion_reason']=='identity_review' for e in rows):
            continue
        cow = next(iter(cows))
        try:
            x,m,q = extract_native(title,root/source['path'],source['asset_id'],cow,pack)
            roles = [candidate_role(z,rows,CODES[title]) for z in m.spot_s]
            m['role'] = [r[0] for r in roles]
            m['event_id'] = [r[1] for r in roles]
            m['split'] = splits.get(cow,'unassigned')
            xs.append(x)
            ms.append(m)
            audit.append(q)
        except Exception as exc:
            audit.append(dict(session_key=source['asset_id'],cow_group=cow,status='REVIEW',error=str(exc)))
        print(json.dumps(dict(head=title,current=i+1,total=len(sources))),flush=True)
    x = pd.concat(xs,ignore_index=True) if xs else pd.DataFrame()
    m = pd.concat(ms,ignore_index=True) if ms else pd.DataFrame(columns=['candidate_id','session_key','cow_group','spot_s','role','event_id','split'])
    result = save_consumer(root/'事件识别'/title,title,x,m,audit)
    matched = set(m.loc[m.role.eq('P'),'event_id'])
    unmatched = [e for e in events if e['code']==CODES[title] and e['training_eligible'] and e['event_id'] not in matched]
    pd.DataFrame(unmatched).to_csv(root/'事件识别'/title/'unmatched_events.csv',index=False,encoding='utf-8-sig')
    result['unmatched_events'] = len(unmatched)
    result['source_errors'] = sum(bool(q.get('error')) for q in audit)
    (root/'事件识别'/title/'readiness.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
