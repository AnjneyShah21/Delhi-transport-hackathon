"""Build compact historical route/hour speed profiles from daily GPS SQLite files.

The GPS feed's trip IDs are not guaranteed to align with scheduled GTFS trips,
so these profiles describe observed route speeds, not schedule delay.
"""
import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'processed' / 'gps_speed_profiles.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('paths', nargs='*', help='daily bus_movements_YYYY_MM_DD.db paths')
    parser.add_argument('--archive', help='process daily SQLite files one at a time from the public non-solid .7z archive')
    parser.add_argument('--days', type=int, default=7, help='number of archive days to process (default: first 7 days)')
    parser.add_argument('--scratch', default=str(ROOT / 'data' / 'processed' / 'gps_scratch'))
    args = parser.parse_args()
    paths = [Path(p) for p in args.paths] or sorted(Path(args.scratch).glob('bus_movements_*.db'))
    if not paths and not args.archive:
        parser.error('no daily GPS databases found; extract them one at a time from the archive')

    # Aggregate speed moments and a coarse histogram so median speed is robust
    # without loading tens of millions of rows into memory.
    groups = defaultdict(lambda: {'n': 0, 'sum': 0.0, 'hist': defaultdict(int)})
    files = []

    def consume(path):
        print(f'Processing {path.name}', flush=True)
        con = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)
        con.execute('PRAGMA query_only=ON')
        cursor = con.execute('SELECT route_id, speed, timestamp FROM vehicle_feed WHERE speed > 0 AND speed <= 25 AND timestamp > 0')
        read = 0
        while rows := cursor.fetchmany(100_000):
            for route, speed, stamp in rows:
                try:
                    # The feed timestamps are Unix UTC; convert to Delhi local time.
                    dt = datetime.fromtimestamp(int(stamp), timezone.utc) + timedelta(hours=5, minutes=30)
                    route = str(int(float(route)))
                    speed = float(speed)
                except (ValueError, TypeError, OverflowError, OSError):
                    continue
                day_type = 'weekend' if dt.weekday() >= 5 else 'weekday'
                key = (route, dt.hour, day_type)
                item = groups[key]
                item['n'] += 1
                item['sum'] += speed
                item['hist'][round(speed * 2) / 2] += 1
            read += len(rows)
        con.close()
        files.append({'name': path.name, 'rows_scanned': read})

    if args.archive:
        try:
            import py7zr
        except ImportError as exc:
            parser.error('archive mode requires py7zr (pip install py7zr)')
        scratch = Path(args.scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        with py7zr.SevenZipFile(args.archive, 'r') as archive:
            names = sorted(n for n in archive.getnames() if n.endswith('.db'))
        if args.days < 1:
            parser.error('--days must be at least 1')
        names = names[:args.days]
        for name in names:
            path = scratch / Path(name).name
            if not path.exists():
                with py7zr.SevenZipFile(args.archive, 'r') as archive:
                    archive.extract(path=str(scratch), targets=[name])
            consume(path)
            path.unlink()
            Path(str(path) + '-wal').unlink(missing_ok=True)
            Path(str(path) + '-shm').unlink(missing_ok=True)
            shutil.rmtree(scratch / 'sqltmp', ignore_errors=True)

    profiles = []
    for (route, hour, day_type), item in sorted(groups.items()):
        n = item['n']
        target = (n - 1) // 2
        seen = 0
        median = 0.0
        for speed, count in sorted(item['hist'].items()):
            seen += count
            if seen > target:
                median = speed
                break
        profiles.append({
            'route_id': route, 'hour': hour, 'day_type': day_type,
            'observations': n, 'mean_speed_mps': round(item['sum'] / n, 2),
            'median_speed_mps': round(median, 2),
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        'source': 'Delhi DTC GTFS-RT vehicle position history, October 2026',
        'method': 'Observed reported speeds grouped by GTFS route ID, Delhi local hour, and weekday/weekend; values outside (0,25] m/s excluded; median uses 0.5 m/s bins.',
        'limitations': 'Historical sampled bus speeds, not passenger counts or schedule delay. GPS trip IDs do not consistently map to scheduled GTFS trips.',
        'files_processed': files, 'profiles': profiles,
    }, separators=(',', ':'))
    temporary = OUT.with_suffix('.json.tmp')
    temporary.write_text(payload, encoding='utf-8')
    temporary.replace(OUT)
    print(f'Wrote {len(profiles)} route/hour profiles to {OUT}', flush=True)


if __name__ == '__main__':
    main()
