"""Match public OpenStreetMap bus-stop tags to GTFS stops.

Requires the optional `osmium` Python package. Output is a small OSM-derived
lookup; it is not a substitute for operator-confirmed bus accessibility data.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import osmium
except ImportError as exc:  # pragma: no cover - optional build dependency
    raise SystemExit('Install the optional OSM parser first: python -m pip install osmium') from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PBF = ROOT / 'data' / 'raw' / 'NewDelhi.osm.pbf'
DEFAULT_GTFS = ROOT / 'data' / 'processed' / 'gtfs.sqlite'
DEFAULT_OUTPUT = ROOT / 'data' / 'processed' / 'osm_stop_accessibility.json'
SOURCE_URL = 'https://download3.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf'
LICENSE = 'Open Data Commons Open Database License (ODbL) 1.0'
MATCH_RADIUS_M = 50
BUCKET_M = 100
TAGS_TO_KEEP = (
    'name', 'ref', 'wheelchair', 'wheelchair:description', 'tactile_paving',
    'kerb', 'surface', 'shelter', 'lit', 'bench', 'crossing', 'ramp',
    'width', 'incline',
)


def is_bus_stop(tags):
    return (tags.get('highway') == 'bus_stop'
            or tags.get('amenity') == 'bus_station'
            or (tags.get('public_transport') == 'platform'
                and (tags.get('bus') == 'yes' or tags.get('mode') == 'bus')))


class BusStopHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.features = []

    def _add(self, kind, element_id, tags, lat, lon):
        if not is_bus_stop(tags):
            return
        self.features.append({
            'element_type': kind,
            'element_id': str(element_id),
            'lat': float(lat),
            'lon': float(lon),
            'tags': {key: tags[key] for key in TAGS_TO_KEEP if key in tags},
        })

    def node(self, node):
        tags = dict(node.tags)
        if is_bus_stop(tags):
            self._add('node', node.id, tags, node.location.lat, node.location.lon)

    def way(self, way):
        tags = dict(way.tags)
        if not is_bus_stop(tags):
            return
        points = [(n.location.lat, n.location.lon) for n in way.nodes if n.location.valid()]
        if points:
            self._add('way', way.id, tags,
                      sum(point[0] for point in points) / len(points),
                      sum(point[1] for point in points) / len(points))


def wheelchair_status(tags):
    value = tags.get('wheelchair', '').strip().lower()
    if value in ('yes', 'designated'):
        return 'yes'
    if value in ('no', 'limited'):
        return value
    return 'unknown'


def accessibility_flags(tags):
    return {
        'wheelchair': wheelchair_status(tags),
        'tactile_paving': tags.get('tactile_paving') if tags.get('tactile_paving') in ('yes', 'no') else 'unknown',
        'lighting': tags.get('lit') if tags.get('lit') in ('yes', 'no') else 'unknown',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pbf', type=Path, default=DEFAULT_PBF)
    parser.add_argument('--gtfs', type=Path, default=DEFAULT_GTFS)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--radius-m', type=int, default=MATCH_RADIUS_M)
    args = parser.parse_args()
    if not args.pbf.exists():
        raise SystemExit(f'OSM PBF not found: {args.pbf}')
    if not args.gtfs.exists():
        raise SystemExit(f'GTFS index not found: {args.gtfs}')

    handler = BusStopHandler()
    handler.apply_file(str(args.pbf), locations=True)
    reader = osmium.io.Reader(str(args.pbf))
    header = reader.header()
    snapshot = str(header.get('osmosis_replication_timestamp') or '')
    reader.close()

    con = sqlite3.connect(args.gtfs)
    stops = con.execute('SELECT stop_id,name,lat,lon FROM stops '
                        'WHERE lat IS NOT NULL AND lon IS NOT NULL').fetchall()
    con.close()

    origin_lat = sum(float(row[2]) for row in stops) / max(1, len(stops))
    meters_per_lon = 111_200 * math.cos(math.radians(origin_lat))
    grid = defaultdict(list)
    for feature in handler.features:
        x = feature['lon'] * meters_per_lon
        y = feature['lat'] * 111_200
        feature['_x'], feature['_y'] = x, y
        grid[(math.floor(x / BUCKET_M), math.floor(y / BUCKET_M))].append(feature)

    matches = {}
    for stop_id, name, lat, lon in stops:
        x, y = float(lon) * meters_per_lon, float(lat) * 111_200
        cell = (math.floor(x / BUCKET_M), math.floor(y / BUCKET_M))
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for feature in grid.get((cell[0] + dx, cell[1] + dy), ()):
                    distance = math.hypot(x - feature['_x'], y - feature['_y'])
                    if best is None or distance < best[0]:
                        best = (distance, feature)
        if best is None or best[0] > args.radius_m:
            continue
        feature = best[1]
        flags = accessibility_flags(feature['tags'])
        matches[str(stop_id)] = {
            'gtfs_stop_name': name,
            'osm_element': f"{feature['element_type']}/{feature['element_id']}",
            'distance_m': round(best[0], 1),
            'tags': feature['tags'],
            **flags,
        }

    payload = {
        'source': 'OpenStreetMap via BBBike NewDelhi extract',
        'source_url': SOURCE_URL,
        'license': LICENSE,
        'snapshot_date': snapshot,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'matching_radius_m': args.radius_m,
        'gtfs_stop_count': len(stops),
        'osm_bus_stop_features': len(handler.features),
        'matched_gtfs_stops': len(matches),
        'wheelchair_known_stops': sum(row['wheelchair'] != 'unknown' for row in matches.values()),
        'wheelchair_yes_stops': sum(row['wheelchair'] == 'yes' for row in matches.values()),
        'wheelchair_limited_stops': sum(row['wheelchair'] == 'limited' for row in matches.values()),
        'wheelchair_no_stops': sum(row['wheelchair'] == 'no' for row in matches.values()),
        'tactile_known_stops': sum(row['tactile_paving'] != 'unknown' for row in matches.values()),
        'lighting_known_stops': sum(row['lighting'] != 'unknown' for row in matches.values()),
        'stops': matches,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n',
                           encoding='utf-8')
    print(json.dumps({key: value for key, value in payload.items() if key != 'stops'}, indent=2))
    print(f'Wrote {len(matches)} stop matches to {args.output}')


if __name__ == '__main__':
    main()
