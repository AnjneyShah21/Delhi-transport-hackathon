#!/usr/bin/env python3
"""Index the Delhi static GTFS zip into a queryable local SQLite database."""
from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import zipfile
from pathlib import Path


def parse_time(value: str) -> int | None:
    try:
        h, m, s = value.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (AttributeError, ValueError):
        return None


def rows(zf: zipfile.ZipFile, filename: str):
    with zf.open(filename) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig', newline=''))


def insert_table(con: sqlite3.Connection, zf: zipfile.ZipFile, filename: str, sql: str, transform, batch_size=50000):
    batch = []
    count = 0
    for row in rows(zf, filename):
        value = transform(row)
        if value is None:
            continue
        batch.append(value)
        if len(batch) >= batch_size:
            con.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        con.executemany(sql, batch)
        count += len(batch)
    return count


def build(archive: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    con = sqlite3.connect(destination)
    con.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE routes(route_id TEXT PRIMARY KEY, agency_id TEXT, short_name TEXT, long_name TEXT, description TEXT, route_type INTEGER);
        CREATE TABLE stops(stop_id TEXT PRIMARY KEY, code TEXT, name TEXT, lat REAL, lon REAL);
        CREATE TABLE trips(trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT, headsign TEXT, direction_id TEXT, shape_id TEXT);
        CREATE TABLE stop_times(trip_id TEXT, stop_sequence INTEGER, stop_id TEXT, arrival_sec INTEGER, departure_sec INTEGER, PRIMARY KEY(trip_id,stop_sequence));
        CREATE TABLE shapes(shape_id TEXT, point_sequence INTEGER, lat REAL, lon REAL, PRIMARY KEY(shape_id,point_sequence));
        CREATE TABLE calendar(service_id TEXT PRIMARY KEY, monday INTEGER,tuesday INTEGER,wednesday INTEGER,thursday INTEGER,friday INTEGER,saturday INTEGER,sunday INTEGER,start_date TEXT,end_date TEXT);
        CREATE TABLE feed_info(key TEXT PRIMARY KEY,value TEXT);
    ''')
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        insert_table(con, zf, 'routes.txt', 'INSERT INTO routes VALUES(?,?,?,?,?,?)', lambda r: (
            r.get('route_id'), r.get('agency_id',''), r.get('route_short_name',''), r.get('route_long_name',''), r.get('route_desc',''), int(r.get('route_type') or 3)))
        insert_table(con, zf, 'stops.txt', 'INSERT INTO stops VALUES(?,?,?,?,?)', lambda r: (
            r.get('stop_id'), r.get('stop_code',''), r.get('stop_name',''), float(r['stop_lat']) if r.get('stop_lat') else None, float(r['stop_lon']) if r.get('stop_lon') else None))
        insert_table(con, zf, 'trips.txt', 'INSERT INTO trips VALUES(?,?,?,?,?,?)', lambda r: (
            r.get('trip_id'), r.get('route_id'), r.get('service_id',''), r.get('trip_headsign',''), r.get('direction_id',''), r.get('shape_id','')))
        insert_table(con, zf, 'calendar.txt', 'INSERT INTO calendar VALUES(?,?,?,?,?,?,?,?,?,?)', lambda r: (
            r.get('service_id'), *(int(r.get(day) or 0) for day in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')), r.get('start_date',''), r.get('end_date','')))
        if 'feed_info.txt' in names:
            for row in rows(zf, 'feed_info.txt'):
                con.execute('INSERT OR REPLACE INTO feed_info VALUES(?,?)', (row.get('feed_version','version'), row.get('feed_publisher_name','')))
        print('Indexing stop times…', flush=True)
        count_times = insert_table(con, zf, 'stop_times.txt', 'INSERT INTO stop_times VALUES(?,?,?,?,?)', lambda r: (
            r.get('trip_id'), int(r.get('stop_sequence') or 0), r.get('stop_id'), parse_time(r.get('arrival_time')), parse_time(r.get('departure_time'))))
        print('Indexing route shapes…', flush=True)
        count_shapes = insert_table(con, zf, 'shapes.txt', 'INSERT INTO shapes VALUES(?,?,?,?)', lambda r: (
            r.get('shape_id'), int(r.get('shape_pt_sequence') or 0), float(r.get('shape_pt_lat') or 0), float(r.get('shape_pt_lon') or 0)))
    con.executescript('''
        CREATE INDEX idx_stoptimes_stop ON stop_times(stop_id,departure_sec,trip_id);
        CREATE INDEX idx_stoptimes_trip_stop ON stop_times(trip_id,stop_id,stop_sequence);
        CREATE INDEX idx_trips_route ON trips(route_id);
        CREATE INDEX idx_trips_shape ON trips(shape_id);
        CREATE INDEX idx_shapes_id_sequence ON shapes(shape_id,point_sequence);
        CREATE INDEX idx_stops_name ON stops(name COLLATE NOCASE);
        CREATE TABLE route_stops AS SELECT t.route_id,st.stop_id,MIN(st.stop_sequence) AS first_sequence
            FROM stop_times st JOIN trips t ON t.trip_id=st.trip_id GROUP BY t.route_id,st.stop_id;
        CREATE INDEX idx_route_stops_route ON route_stops(route_id,stop_id);
        CREATE INDEX idx_route_stops_stop ON route_stops(stop_id,route_id);
        ANALYZE;
    ''')
    con.commit()
    counts = {table: con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('routes','stops','trips','stop_times','shapes','route_stops')}
    con.close()
    print(f'Built {destination} from {archive.name}: {counts}; inserted {count_times} stop times and {count_shapes} shape points', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', default='data/raw/delhi_buses_static_gtfs_v1.zip')
    parser.add_argument('--output', default='data/processed/gtfs.sqlite')
    args = parser.parse_args()
    build(Path(args.archive), Path(args.output))
