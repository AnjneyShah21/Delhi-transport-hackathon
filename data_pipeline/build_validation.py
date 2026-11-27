"""Create a chronological, week-held-out rank validation for ticket profiles.

Train: October 1–21; test: October 22–28. The final three October days are
excluded. Metrics compare route-relative ticket activity, not passenger loads.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def build(archive: Path, output: Path) -> dict:
    route_frames = []
    hourly_frames = []
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
                dt = pd.to_datetime(chunk['created_at'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
                chunk['date'] = dt.dt.strftime('%Y-%m-%d')
                chunk['hour'] = dt.dt.hour
                chunk['day_type'] = dt.dt.day_of_week.ge(5).map({True: 'weekend', False: 'weekday'})
                chunk['route'] = chunk['route'].astype('string').str.strip().str.upper()
                chunk['proxy'] = pd.to_numeric(chunk['passenger_count'], errors='coerce').fillna(1).clip(lower=0)
                chunk = chunk.dropna(subset=['date', 'hour', 'route'])
                chunk = chunk[(chunk['route'] != '') & chunk['date'].str.startswith('2026-10-')]
                if chunk.empty:
                    continue
                chunk['period'] = chunk['date'].str[-2:].astype(int).map(
                    lambda day: 'train' if 1 <= day <= 21 else 'test' if 22 <= day <= 28 else 'excluded')
                route_frames.append(chunk[chunk['period'] != 'excluded'].groupby(
                    ['route', 'hour', 'day_type', 'period'], observed=True
                ).agg(tickets=('route', 'size'), proxy=('proxy', 'sum')).reset_index())
                hourly_frames.append(chunk.groupby(
                    ['date', 'hour', 'day_type'], observed=True
                ).agg(tickets=('route', 'size'), proxy=('proxy', 'sum')).reset_index())

    if not route_frames:
        raise ValueError('No usable October ticket records found')
    route = pd.concat(route_frames, ignore_index=True).groupby(
        ['route', 'hour', 'day_type', 'period'], as_index=False
    ).sum(numeric_only=True)
    train_days = {d.strftime('%Y-%m-%d') for d in pd.date_range('2026-10-01', '2026-10-21')}
    test_days = {d.strftime('%Y-%m-%d') for d in pd.date_range('2026-10-22', '2026-10-28')}
    day_counts = {}
    for split, subset in (('train', train_days), ('test', test_days)):
        for kind, is_weekend in (('weekday', False), ('weekend', True)):
            day_counts[f'{split}_{kind}'] = sum(
                (pd.Timestamp(d).day_of_week >= 5) == is_weekend for d in subset
            )
    wide = route.pivot_table(index=['route', 'hour', 'day_type'], columns='period', values=['tickets', 'proxy'], fill_value=0)
    wide.columns = [f'{measure}_{period}' for measure, period in wide.columns]
    wide = wide.reset_index()
    wide['train_daily_proxy'] = wide['proxy_train'] / wide['day_type'].map({'weekday': day_counts['train_weekday'], 'weekend': day_counts['train_weekend']}).replace(0, 1)
    wide['test_daily_proxy'] = wide['proxy_test'] / wide['day_type'].map({'weekday': day_counts['test_weekday'], 'weekend': day_counts['test_weekend']}).replace(0, 1)

    bands = []
    for (hour, day_type), group in wide.groupby(['hour', 'day_type']):
        supported = group[(group['tickets_train'] >= 8) & (group['tickets_test'] >= 3)].copy()
        if len(supported) < 5:
            continue
        train_rank = supported['train_daily_proxy'].rank(method='average', pct=True) * 100
        test_rank = supported['test_daily_proxy'].rank(method='average', pct=True) * 100
        correlation = train_rank.corr(test_rank, method='pearson')
        bands.append({
            'hour': int(hour), 'day_type': str(day_type), 'routes_compared': int(len(supported)),
            'percentile_mae': round(float((train_rank - test_rank).abs().mean()), 1),
            'spearman_rho': round(float(correlation), 3) if pd.notna(correlation) else None,
        })

    valid = [b for b in bands if b['spearman_rho'] is not None]
    hourly = pd.concat(hourly_frames, ignore_index=True).groupby(
        ['date', 'hour', 'day_type'], as_index=False
    ).sum(numeric_only=True)
    daily = hourly.groupby(['date', 'day_type'], as_index=False).agg(
        tickets=('tickets', 'sum'), proxy=('proxy', 'sum')
    )
    anomalies = []
    for day_type, group in daily.groupby('day_type'):
        baseline = float(group['tickets'].median())
        mad = float((group['tickets'] - baseline).abs().median())
        threshold = max(baseline * .2, mad * 2.5)
        if threshold <= 0:
            continue
        for row in group.itertuples(index=False):
            deviation = float(row.tickets) - baseline
            if abs(deviation) >= threshold:
                anomalies.append({
                    'date':str(row.date), 'day_type':str(day_type),
                    'ticket_records':int(row.tickets), 'baseline_records':round(baseline),
                    'deviation_percent':round(deviation / baseline * 100, 1) if baseline else None,
                    'direction':'high' if deviation > 0 else 'low',
                })
    all_days = pd.date_range('2026-10-01', '2026-10-31')
    days_by_type = {
        'weekend': sum(d.day_of_week >= 5 for d in all_days),
        'weekday': sum(d.day_of_week < 5 for d in all_days),
    }
    curve = hourly.groupby(['hour', 'day_type'], as_index=False).agg(
        tickets=('tickets', 'sum'), proxy=('proxy', 'sum')
    )
    curve['days'] = curve['day_type'].map(days_by_type).replace(0, 1)
    curve['tickets_per_day'] = curve['tickets'] / curve['days']
    curve['proxy_per_day'] = curve['proxy'] / curve['days']

    result = {
        'source': 'October 2026 ONDC ticket sample',
        'method': 'Train route/hour/day-type activity on October 1–21; compare within-band route percentile ranks against October 22–28. Require at least 8 training and 3 test tickets per route-hour cell. Mean daily passenger_count proxy is used only for ranking.',
        'train_period': '2026-10-01 through 2026-10-21',
        'test_period': '2026-10-22 through 2026-10-28',
        'excluded_period': '2026-10-29 through 2026-10-31',
        'days_by_split': day_counts,
        'bands_scored': len(valid),
        'route_hour_day_cells': int(sum(b['routes_compared'] for b in valid)),
        'mean_percentile_mae': round(sum(b['percentile_mae'] for b in valid) / len(valid), 1) if valid else None,
        'mean_spearman_rho': round(sum(b['spearman_rho'] for b in valid) / len(valid), 3) if valid else None,
        'bands': bands,
        'hourly_activity': [{
            'hour': int(r.hour), 'day_type': str(r.day_type),
            'tickets_per_day': round(float(r.tickets_per_day), 1),
            'passenger_proxy_per_day': round(float(r.proxy_per_day), 1),
        } for r in curve.itertuples(index=False)],
        'daily_ticket_anomalies': sorted(anomalies, key=lambda x:x['date']),
        'daily_anomaly_method': 'Flag a day when its ticket count differs from the weekday/weekend median by at least 20% or 2.5 median absolute deviations. This detects sample-volume outliers only; it does not identify events, weather, or actual passenger loads.',
        'limitations': 'This is a temporal rank-stability check on sampled ticket activity, not validation against onboard passenger counts or true crowding. Ticket coverage is partial; route/hour cells without enough test data are omitted. It does not validate live buses, GPS travel times, or Google Maps estimates.',
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.tmp')
    temp.write_text(json.dumps(result, separators=(',', ':')), encoding='utf-8')
    temp.replace(output)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', default='data/raw/October-Delhi.csv.zip')
    parser.add_argument('--output', default='data/processed/crowd_validation.json')
    args = parser.parse_args()
    report = build(Path(args.archive), Path(args.output))
    print(json.dumps({k: report[k] for k in ('train_period', 'test_period', 'bands_scored', 'route_hour_day_cells', 'mean_percentile_mae', 'mean_spearman_rho')}, indent=2))
