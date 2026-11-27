#!/usr/bin/env python3
"""Build compact route/time crowd profiles from the October ONDC ticket archive.

Ticket volume is used only as a relative proxy. This script does not infer
on-board passenger counts or route connectivity.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pandas as pd


def build(archive: Path, output: Path) -> dict:
    frames: list[pd.DataFrame] = []
    od_frames: list[pd.DataFrame] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        member = next((n for n in zf.namelist() if n.endswith('.csv') and '/._' not in n), None)
        if not member:
            raise ValueError('No CSV member found in ticket archive')
        with zf.open(member) as stream:
            for chunk in pd.read_csv(stream, chunksize=150_000, low_memory=False):
                chunk.columns = [str(c).strip().lower() for c in chunk.columns]
                needed = {'route', 'created_at', 'passenger_count'}
                if not needed.issubset(chunk.columns):
                    raise ValueError(f'Missing expected columns: {sorted(needed - set(chunk.columns))}')
                chunk['route'] = chunk['route'].astype('string').str.strip().str.upper()
                chunk['passengers'] = pd.to_numeric(chunk['passenger_count'], errors='coerce').fillna(1).clip(lower=0)
                dt = pd.to_datetime(chunk['created_at'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
                chunk['hour'] = dt.dt.hour
                chunk['day_type'] = dt.dt.day_of_week.ge(5).map({True: 'weekend', False: 'weekday'})
                chunk = chunk.dropna(subset=['route', 'hour'])
                chunk = chunk[chunk['route'].ne('')]
                if not chunk.empty:
                    frames.append(chunk.groupby(['route', 'hour', 'day_type'], observed=True).agg(
                        tickets=('route', 'size'), passenger_proxy=('passengers', 'sum')
                    ).reset_index())
                    if {'start_stop_name', 'end_stop_name'}.issubset(chunk.columns):
                        chunk['start_stop_name'] = chunk['start_stop_name'].astype('string').str.strip()
                        chunk['end_stop_name'] = chunk['end_stop_name'].astype('string').str.strip()
                        od = chunk.dropna(subset=['start_stop_name', 'end_stop_name'])
                        od = od[od['start_stop_name'].ne('') & od['end_stop_name'].ne('')]
                        if not od.empty:
                            od_frames.append(od.groupby(['start_stop_name', 'end_stop_name', 'route', 'hour', 'day_type'], observed=True).agg(
                                tickets=('route', 'size'), passenger_proxy=('passengers', 'sum')
                            ).reset_index())
                total += len(chunk)
    if not frames:
        raise ValueError('No usable ticket records found')
    grouped = pd.concat(frames, ignore_index=True).groupby(['route', 'hour', 'day_type'], as_index=False).sum(numeric_only=True)
    # Stabilize small/sparse groups with a route-wide median, then rank routes
    # within the same time band. Ranks are relative and deliberately not counts.
    route_medians = grouped.groupby('route')['passenger_proxy'].median()
    grouped['smoothed_proxy'] = grouped['passenger_proxy'].astype(float)
    sparse = grouped['tickets'] < 8
    grouped.loc[sparse, 'smoothed_proxy'] = grouped.loc[sparse, 'route'].map(route_medians)
    grouped['crowd_score'] = grouped.groupby(['hour', 'day_type'])['smoothed_proxy'].rank(pct=True, method='average').mul(100).round().astype(int)
    grouped['confidence'] = (0.35 + 0.12 * (grouped['tickets'].clip(upper=100) / 100).pow(0.5)).clip(upper=0.8).round(2)
    grouped = grouped.sort_values(['route', 'day_type', 'hour'])
    routes = sorted(grouped['route'].unique().tolist())
    # Compare total daily ticket activity across hours to show a useful
    # network-level quiet-hour curve. Median within-hour route rank would be
    # nearly 50 by construction and would not show peak/off-peak patterns.
    hourly = grouped.groupby(['hour', 'day_type'], as_index=False).agg(
        ticket_records=('tickets', 'sum')
    )
    hourly['score'] = hourly.groupby('day_type')['ticket_records'].rank(pct=True, method='average').mul(100).round().astype(int)
    route_totals = grouped.groupby('route', as_index=False).agg(ticket_records=('tickets', 'sum'), passenger_proxy=('passenger_proxy', 'sum'))
    route_totals = route_totals.sort_values('ticket_records', ascending=False)
    route_rank = route_totals.set_index('route')['ticket_records'].rank(pct=True).mul(100).round().astype(int).to_dict()
    od_grouped = pd.concat(od_frames, ignore_index=True).groupby(
        ['start_stop_name', 'end_stop_name', 'route', 'hour', 'day_type'], as_index=False
    ).sum(numeric_only=True) if od_frames else pd.DataFrame()
    payload = {
        'source': 'Transport Stack / IIIT Delhi October 2026 ONDC ticket sample',
        'generated_from': archive.name,
        'records_processed': int(total),
        'route_count': int(len(routes)),
        'routes': routes,
        'profiles': [{
            'route': str(r.route), 'hour': int(r.hour), 'day_type': str(r.day_type),
            'score': int(r.crowd_score), 'confidence': float(r.confidence),
            'ticket_records': int(r.tickets)
        } for r in grouped.itertuples(index=False)],
        'hourly': [{
            'hour': int(r.hour), 'day_type': str(r.day_type), 'score': int(r.score),
            'ticket_records': int(r.ticket_records)
        } for r in hourly.itertuples(index=False)],
        'top_routes': [{
            'route': str(r.route), 'ticket_records': int(r.ticket_records),
            'relative_score': int(route_rank[r.route])
        } for i, r in enumerate(route_totals.head(30).itertuples(index=False))],
        'od_pairs': [{
            'from': str(r.start_stop_name), 'to': str(r.end_stop_name), 'route': str(r.route),
            'hour': int(r.hour), 'day_type': str(r.day_type), 'ticket_records': int(r.tickets),
            'passenger_proxy': float(r.passenger_proxy)
        } for r in od_grouped.sort_values('tickets', ascending=False).head(50000).itertuples(index=False)] if not od_grouped.empty else [],
        'stops': sorted(set(od_grouped['start_stop_name'].dropna().astype(str)) | set(od_grouped['end_stop_name'].dropna().astype(str))) if not od_grouped.empty else [],
        'limitations': 'Relative ticket-derived proxy, not onboard passenger counts. No GTFS connectivity or live vehicle locations.'
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', default='data/raw/October-Delhi.csv.zip')
    parser.add_argument('--output', default='data/processed/crowd_profiles.json')
    args = parser.parse_args()
    result = build(Path(args.archive), Path(args.output))
    print(f"Processed {result['records_processed']:,} tickets across {result['route_count']:,} route IDs → {args.output}")
