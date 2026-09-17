"""Label-free point suppression within continuous segments."""
import numpy as np
import pandas as pd

def nms(meta,score,min_separation=20):
    """Full-session ranked point NMS, never joins across valid sensor segments."""
    rows=[]
    for (key,seg),g in meta.groupby(['session_key','segment_start_s'],sort=True):
        order=np.argsort(-score[g.index.values],kind='stable');used=[]
        for ix in order:
            r=g.iloc[ix];t=float(r.spot_s)
            if any(abs(t-q)<min_separation for q in used):continue
            used.append(t);rows.append(dict(session_key=key,cow_group=str(r.cow_group),spot_s=t,score=float(score[r.name]),candidate_id=r.candidate_id,segment_start_s=float(seg)))
    return pd.DataFrame(rows).sort_values(['cow_group','score'],ascending=[True,False]).reset_index(drop=True)

