"""Relative crowd baseline with sparse-group fallback and confidence."""
from __future__ import annotations
import numpy as np
import pandas as pd

GROUP = ['route_id','segment_id','hour','day_type']

def fit_profile(observations: pd.DataFrame, min_group: int = 8) -> pd.DataFrame:
    """Input requires route_id, segment_id, hour, day_type and load_proxy columns.

    load_proxy is a ticket-derived relative proxy, never an absolute passenger count.
    Sparse groups fall back route/day/hour -> route/day -> global.
    """
    needed = set(GROUP + ['load_proxy'])
    missing = needed - set(observations.columns)
    if missing: raise ValueError(f'Missing columns: {sorted(missing)}')
    d = observations.dropna(subset=['load_proxy']).copy()
    if d.empty: return pd.DataFrame(columns=GROUP+['load_median','crowd_score','confidence','sample_count'])
    d['load_proxy'] = pd.to_numeric(d.load_proxy, errors='coerce')
    d = d.dropna(subset=['load_proxy'])
    if d.empty: return pd.DataFrame(columns=GROUP+['load_median','crowd_score','confidence','sample_count'])
    keys = d[GROUP].drop_duplicates()
    global_med = d.load_proxy.median()
    rows=[]
    for key in keys.itertuples(index=False, name=None):
        mask=np.ones(len(d),dtype=bool)
        for col,val in zip(GROUP,key): mask &= d[col].eq(val).to_numpy()
        g=d.loc[mask]
        route,seg,hour,day=key
        if len(g)>=min_group: source=g; confidence=min(.98,.45+.05*np.log1p(len(g)))
        else:
            source=d[(d.route_id==route)&(d.hour==hour)&(d.day_type==day)]
            if source.empty: source=d[(d.route_id==route)&(d.day_type==day)]
            if source.empty: source=d
            confidence=min(.65,.2+.08*np.log1p(len(g)))
        rows.append((*key,float(source.load_proxy.median()),confidence,int(len(g))))
    out=pd.DataFrame(rows,columns=GROUP+['load_median','confidence','sample_count'])
    # Percentile within route and day-type profile keeps labels relative.
    counts=out.groupby(['route_id','day_type']).load_median.transform('size')
    ranks=out.groupby(['route_id','day_type']).load_median.rank(pct=True,method='average').mul(100)
    out['crowd_score']=ranks.where(counts>1,50).round().astype(int)
    return out

def crowd_label(score: float) -> dict:
    level='low' if score<35 else 'medium' if score<68 else 'high'
    return {'level':level,'en':{'low':'Low crowd','medium':'Medium crowd','high':'High crowd'}[level],
            'hi':{'low':'Kam bheed','medium':'Thodi bheed','high':'Bahut bheed'}[level]}

def seat_chance(score: float, board_position: float) -> str:
    """board_position ranges 0 (route start) to 1 (route end)."""
    adjusted=score-12*max(0,min(1,1-board_position))
    return 'Good chance of a seat' if adjusted<35 else 'Maybe' if adjusted<68 else 'Likely standing'
