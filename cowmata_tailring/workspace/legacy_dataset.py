"""Align trusted historical feature banks to current mother labels and cow folds.

Standalone Python 3.8 worker. Original tables are read only. No old cow assignment
or historical split can override a pending identity in the current mother set.
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from algorithm_dataset import CODES, candidate_role, save_consumer


def historical_bank(root,title,pack):
    folder = root/title
    if title == '起立':
        table = pd.read_csv(folder/'training_candidates.csv',dtype={'cow_group':str})
        columns = [c for c in table if c not in ['candidate_id','session_key','cow_group','spot_s','parent_event_id','target']]
        x,m = table[columns],table.drop(columns=columns).copy()
        m['role'] = np.where(m.target.eq(1),'P','N')
        m['legacy_event_id'] = m.parent_event_id
    elif title == '卧倒':
        z = np.load(folder/'training/features.npz',allow_pickle=False)
        x = pd.DataFrame(z['X'],columns=z['columns'])
        m = pd.read_csv(folder/'training/candidates.csv',dtype={'cow_group':str}).fillna('')
        if list(z['candidate_id']) != list(m.candidate_id):
            raise ValueError('Historical candidate order mismatch')
        m['role'] = np.where(m.local_positive,'P',np.where(m.explicit_state_negative,'N','U'))
        m['legacy_event_id'] = m.local_bag
    elif title == '排尿':
        table = pd.read_pickle(folder/'training/windows.pkl')
        columns = json.loads((pack/title/'feature_columns.json').read_text(encoding='utf-8'))
        x,m = table[columns],table.drop(columns=columns).copy()
        m['role'] = np.where(m.train_target.eq(1),'P','PROXY')
        m['spot_s'] = m.center_s
        m['candidate_id'] = [str(i) for i in range(len(m))]
        m['legacy_event_id'] = m.positive_event_id
    elif title == '抬尾':
        x = pd.read_pickle(folder/'training/features.pkl')
        m = pd.read_pickle(folder/'training/points.pkl').copy()
        if not x.index.equals(m.index):
            raise ValueError('Historical point order mismatch')
        m['legacy_event_id'] = m.positive_event_id
    elif title == '甩尾':
        z = np.load(folder/'training_features.npz',allow_pickle=False)
        x = pd.DataFrame(z['x'],columns=z['columns'])
        m = pd.read_csv(folder/'metadata/sample_manifest.csv',dtype={'cow_group':str}).fillna('')
        if list(z['window_id']) != list(m.window_id):
            raise ValueError('Historical window order mismatch')
        m['candidate_id'] = m.window_id
        m['spot_s'] = (m.core_start_ms+m.core_end_ms)/2000
        m['role'] = np.where(m.label.eq('known_positive'),'P','PROXY')
        m['legacy_event_id'] = m.event_id
    return x.reset_index(drop=True),m.reset_index(drop=True)


def merge_historical(dataset, historical, pack, *, allow_proxy=False):
    dataset,historical,pack = Path(dataset),Path(historical),Path(pack)
    root = historical/'02_数据集' if (historical/'02_数据集').is_dir() else historical
    raw_index = pd.read_csv(root/'原始JSON/manifest.csv',dtype={'cow_group':str}).set_index('session_key')
    events = [json.loads(line) for line in (dataset/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    sources = {r['asset_id']:r for r in [json.loads(line) for line in (dataset/'sources.jsonl').read_text(encoding='utf-8').splitlines()]}
    splits = json.loads((dataset/'cow-splits.json').read_text(encoding='utf-8'))
    results = []
    for title,code in CODES.items():
        x,m = historical_bank(root,title,pack)
        joined = []
        for i,row in m.iterrows():
            key = row.session_key
            original_role = row.role
            item = dict(candidate_id='legacy:'+str(row.candidate_id),legacy_candidate_id=str(row.candidate_id),
                session_key='',cow_group=str(row.cow_group),spot_s=float(row.spot_s),role='IGNORE',event_id='',
                split='review',legacy_role=original_role,legacy_event_id=str(row.get('legacy_event_id','')),
                legacy_source=str(root/title),legacy_row=i,exclusion_reason='missing_source')
            if key in raw_index.index:
                raw = raw_index.loc[key]
                asset = raw.sha256
                item['session_key'] = asset
                source = sources.get(asset,{})
                rows = [e for e in events if e['source_asset_id']==asset]
                consistent = source.get('identity_eligible',False) and str(source.get('cow_id'))==str(row.cow_group)
                if consistent:
                    role,event_id = candidate_role(float(row.spot_s),rows,code)
                    if original_role=='P':
                        item['role'],item['event_id'] = ('P',event_id) if role=='P' else ('IGNORE','')
                    elif original_role=='N':
                        item['role'] = 'N' if role not in {'P','IGNORE'} else 'IGNORE'
                    elif original_role=='U':
                        item['role'] = 'U' if role=='U' else 'IGNORE'
                    elif original_role=='PROXY' and allow_proxy:
                        item['role'] = 'PROXY' if role=='U' else 'IGNORE'
                    item['split'] = splits.get(str(row.cow_group),'unassigned')
                    item['exclusion_reason'] = '' if item['role']!='IGNORE' else 'label_version_or_proxy_review'
                    # Proxy negatives never become evaluation truth.
                    if item['role']=='PROXY' and item['split']!='train':
                        item['role'] = 'U'
                else:
                    item['exclusion_reason'] = 'identity_review'
            joined.append(item)
        aligned = pd.DataFrame(joined)
        directory = dataset/'历史算法对齐'/title
        result = save_consumer(directory,title,x,aligned,[])
        # Preserve every old row and exact feature value, including review rows.
        aligned.to_csv(directory/'legacy_to_mother.csv',index=False,encoding='utf-8-sig')
        result['legacy_rows'] = len(aligned)
        result['excluded_rows'] = int(aligned.role.eq('IGNORE').sum())
        result['legacy_source_sha256'] = hashlib.sha256((root/'原始JSON/manifest.csv').read_bytes()).hexdigest()
        native = dataset/'事件识别'/title
        z = np.load(native/'candidate_features.npz',allow_pickle=False)
        fresh = pd.DataFrame(z['X'],columns=z['columns'])
        fresh_meta = pd.read_csv(native/'candidate_manifest.csv',dtype={'cow_group':str}).fillna('')
        if list(fresh.columns) != list(x.columns):
            raise ValueError('Feature schema differs; cannot merge '+title)
        combined_x = pd.concat([fresh,x],ignore_index=True)
        combined_m = pd.concat([fresh_meta,aligned],ignore_index=True)
        # Keep one record per source/point/role. Old curated negatives can replace
        # an unknown candidate but never override a current positive or conflict.
        combined_m['join_key'] = combined_m.session_key+':'+(combined_m.spot_s*1000).round().astype(int).astype(str)
        ranked = combined_m.assign(priority=combined_m.role.map({'P':0,'N':1,'PROXY':2,'U':3,'IGNORE':4}))
        # A current pending annotation cannot be reinstated by a historical copy.
        pending = set(fresh_meta.loc[fresh_meta.role.eq('IGNORE')].apply(
            lambda r:str(r.session_key)+':'+str(round(r.spot_s*1000)),axis=1))
        ranked.loc[ranked.join_key.isin(pending),'role'] = 'IGNORE'
        combined_m.loc[combined_m.join_key.isin(pending),'role'] = 'IGNORE'
        keep = ranked.sort_values('priority',kind='stable').drop_duplicates('join_key').index.sort_values()
        combined = save_consumer(native,title,combined_x.loc[keep].reset_index(drop=True),
                                 combined_m.loc[keep].reset_index(drop=True),[])
        result['combined'] = combined
        results.append(result)
    (dataset/'历史算法对齐/归一化结果.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return results


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    merge_historical(request['dataset'],request['historical'],request['pack'],allow_proxy=request.get('allow_proxy',False))
