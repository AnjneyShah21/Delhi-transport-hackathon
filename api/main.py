from datetime import date
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title='Delhi Ki Public API', description='Delhi Ki Public — Know the crowd before you board. Crowd scores are relative estimates, not passenger counts.', version='0.3.0')
ROOT = Path(__file__).resolve().parent.parent
app.mount('/vendor/leaflet', StaticFiles(directory=ROOT / 'frontend' / 'vendor' / 'leaflet'), name='leaflet')
PROFILE_FILE = ROOT / 'data' / 'processed' / 'crowd_profiles.json'
GTFS_FILE = ROOT / 'data' / 'processed' / 'gtfs.sqlite'
GPS_PROFILE_FILE = ROOT / 'data' / 'processed' / 'gps_speed_profiles.json'
VALIDATION_FILE = ROOT / 'data' / 'processed' / 'crowd_validation.json'
try:
    PROFILE = json.loads(PROFILE_FILE.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    PROFILE = None
PROFILE_INDEX = {
    (p['route'], p['hour'], p['day_type']): p
    for p in (PROFILE or {}).get('profiles', [])
}
try:
    GPS_PROFILE = json.loads(GPS_PROFILE_FILE.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    GPS_PROFILE = None
GPS_PROFILE_INDEX = {
    (str(p['route_id']), int(p['hour']), p['day_type']): p
    for p in (GPS_PROFILE or {}).get('profiles', [])
}
try:
    VALIDATION = json.loads(VALIDATION_FILE.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    VALIDATION = None


def gps_profile_for(route_id, hour, day_type):
    if not GPS_PROFILE:
        return None
    route_id = str(route_id).strip()
    found = GPS_PROFILE_INDEX.get((route_id, int(hour), day_type))
    if found:
        return found
    rows = [p for p in GPS_PROFILE.get('profiles', [])
            if str(p['route_id']) == route_id and p['day_type'] == day_type]
    if not rows:
        return None
    nearest = min(rows, key=lambda p: abs(int(p['hour']) - int(hour)))
    return {**nearest, 'observations': max(1, round(nearest['observations'] * .65)), 'hour_matched': False}


def geometry_distance_km(points):
    from math import asin, cos, radians, sin, sqrt
    distance = 0.0
    for a, b in zip(points, points[1:]):
        lat1, lon1 = radians(float(a[0])), radians(float(a[1]))
        lat2, lon2 = radians(float(b[0])), radians(float(b[1]))
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        distance += 6371.0088 * 2 * asin(min(1.0, sqrt(h)))
    return distance


def gtfs_connection():
    if not GTFS_FILE.exists():
        return None
    con = sqlite3.connect(f'file:{GTFS_FILE}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_accessibility_coverage(con):
    if not con:
        return {'available': False, 'wheelchair_stop_fields': False, 'wheelchair_trip_fields': False,
                'pathways_available': False, 'note': 'Static bus GTFS is not indexed.'}
    try:
        stop_stats = con.execute('''SELECT COUNT(*) total,
            SUM(CASE WHEN wheelchair_boarding IN (1,2) THEN 1 ELSE 0 END) known,
            SUM(CASE WHEN wheelchair_boarding=1 THEN 1 ELSE 0 END) accessible
            FROM stops''').fetchone()
        trip_stats = con.execute('''SELECT COUNT(*) total,
            SUM(CASE WHEN wheelchair_accessible IN (1,2) THEN 1 ELSE 0 END) known,
            SUM(CASE WHEN wheelchair_accessible=1 THEN 1 ELSE 0 END) accessible
            FROM trips''').fetchone()
        pathway_count = con.execute('SELECT COUNT(*) FROM pathways').fetchone()[0]
        elevator_count = con.execute('SELECT COUNT(*) FROM pathways WHERE pathway_mode=5').fetchone()[0]
        stair_count = con.execute('SELECT COUNT(*) FROM pathways WHERE pathway_mode=2').fetchone()[0]
    except sqlite3.Error:
        return {'available': False, 'wheelchair_stop_fields': False, 'wheelchair_trip_fields': False,
                'pathways_available': False, 'note': 'GTFS accessibility fields are not available in this index.'}
    stop_known, trip_known = int(stop_stats['known'] or 0), int(trip_stats['known'] or 0)
    complete = stop_known > 0 and trip_known > 0
    return {
        'available': complete,
        'wheelchair_stop_fields': stop_known > 0,
        'wheelchair_trip_fields': trip_known > 0,
        'wheelchair_accessible_stops': int(stop_stats['accessible'] or 0),
        'wheelchair_accessible_trips': int(trip_stats['accessible'] or 0),
        'stops_with_accessibility_status': stop_known,
        'trips_with_accessibility_status': trip_known,
        'pathways_available': pathway_count > 0,
        'pathways': pathway_count,
        'elevator_pathways': elevator_count,
        'stair_pathways': stair_count,
        'note': 'Unknown accessibility values are not treated as accessible.' if complete else 'Current feed does not provide enough explicit bus and stop accessibility fields to verify a wheelchair-accessible journey.'
    }


def stop_wheelchair_access(con, stop_id):
    row = con.execute('SELECT wheelchair_boarding,parent_station FROM stops WHERE stop_id=?', (stop_id,)).fetchone()
    if not row:
        return None
    value = int(row['wheelchair_boarding'] or 0)
    if value in (1, 2):
        return value == 1
    parent = row['parent_station']
    if parent:
        inherited = con.execute('SELECT wheelchair_boarding FROM stops WHERE stop_id=?', (parent,)).fetchone()
        if inherited and int(inherited['wheelchair_boarding'] or 0) in (1, 2):
            return int(inherited['wheelchair_boarding']) == 1
    return None


def leg_wheelchair_access(con, leg):
    trip = con.execute('SELECT wheelchair_accessible FROM trips WHERE trip_id=?', (leg['trip_id'],)).fetchone()
    trip_access = int(trip['wheelchair_accessible'] or 0) if trip else 0
    from_access = stop_wheelchair_access(con, leg['from_id'])
    to_access = stop_wheelchair_access(con, leg['to_id'])
    values = [trip_access == 1, from_access is True, to_access is True]
    if all(values):
        status = 'verified_accessible'
    elif trip_access == 2 or from_access is False or to_access is False:
        status = 'not_accessible'
    else:
        status = 'unknown'
    return {'status': status, 'trip_accessible': trip_access == 1 if trip_access in (1, 2) else None,
            'from_stop_accessible': from_access, 'to_stop_accessible': to_access}


def itinerary_wheelchair_access(con, itinerary):
    legs = [leg_wheelchair_access(con, leg) for leg in itinerary['legs']]
    status = 'verified_accessible' if legs and all(x['status'] == 'verified_accessible' for x in legs) else (
        'not_accessible' if any(x['status'] == 'not_accessible' for x in legs) else 'unknown')
    return {'status': status, 'legs': legs}


def stop_matches(con, query: str, limit: int = 12):
    q = query.strip()
    if not con or not q:
        return []
    rows = con.execute('''SELECT stop_id,code AS stop_code,name,lat,lon,
        CASE WHEN lower(name)=lower(?) THEN 0 WHEN lower(name) LIKE lower(?) THEN 1 ELSE 2 END AS rank
        FROM stops WHERE lower(name) LIKE lower(?) ORDER BY rank,name LIMIT ?''',
        (q, q + '%', '%' + q + '%', limit)).fetchall()
    return [dict(r) for r in rows]


def active_services(con, chosen_date):
    weekday = chosen_date.weekday()
    fields = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')
    ymd = chosen_date.strftime('%Y%m%d')
    sql = f'''SELECT service_id FROM calendar WHERE {fields[weekday]}=1 AND start_date<=? AND end_date>=?'''
    return [r['service_id'] for r in con.execute(sql, (ymd, ymd)).fetchall()]


def stop_lookup(con, query):
    matches = stop_matches(con, query, 24)
    return [s['stop_id'] for s in matches], matches


def leg_geometry(con, trip_id, from_stop_id, to_stop_id, shape_id):
    from_stop = con.execute('SELECT lat,lon FROM stops WHERE stop_id=?', (from_stop_id,)).fetchone()
    to_stop = con.execute('SELECT lat,lon FROM stops WHERE stop_id=?', (to_stop_id,)).fetchone()
    points = []
    if shape_id:
        points = [(r['lat'], r['lon']) for r in con.execute('SELECT lat,lon FROM shapes WHERE shape_id=? ORDER BY point_sequence', (shape_id,))]
    if len(points) >= 2 and from_stop and to_stop:
        def nearest(point):
            lat, lon = point['lat'], point['lon']
            return min(range(len(points)), key=lambda i: (points[i][0]-lat)**2 + (points[i][1]-lon)**2)
        a, b = nearest(from_stop), nearest(to_stop)
        points = points[a:b+1] if a <= b else list(reversed(points[b:a+1]))
    if len(points) < 2:
        seq = con.execute('''SELECT s.lat,s.lon FROM stop_times st JOIN stops s USING(stop_id)
            WHERE st.trip_id=? AND st.stop_sequence BETWEEN
            (SELECT stop_sequence FROM stop_times WHERE trip_id=? AND stop_id=? ORDER BY stop_sequence LIMIT 1) AND
            (SELECT stop_sequence FROM stop_times WHERE trip_id=? AND stop_id=? ORDER BY stop_sequence LIMIT 1)
            ORDER BY st.stop_sequence''', (trip_id,trip_id,from_stop_id,trip_id,to_stop_id)).fetchall()
        points = [(r['lat'],r['lon']) for r in seq if r['lat'] is not None and r['lon'] is not None]
    return [[float(lat),float(lon)] for lat,lon in points]


def find_itineraries(con, origin_ids, destination_ids, target_sec, services):
    if not origin_ids or not destination_ids or not services:
        return []
    origin_marks = ','.join('?' for _ in origin_ids)
    dest_marks = ','.join('?' for _ in destination_ids)
    service_clause = ''
    service_args = []
    if services:
        service_clause = f' AND t.service_id IN ({",".join("?" for _ in services)})'
        service_args = services
    direct_sql = f'''SELECT t.trip_id,t.route_id,t.service_id,t.headsign,t.shape_id,r.short_name,r.long_name,
        a.stop_id AS from_id,a.departure_sec AS departure_sec,b.stop_id AS to_id,b.arrival_sec AS arrival_sec,
        a.stop_sequence AS from_seq,b.stop_sequence AS to_seq,fs.name AS from_name,ts.name AS to_name
        FROM stop_times a JOIN stop_times b ON b.trip_id=a.trip_id AND b.stop_sequence>a.stop_sequence
        JOIN trips t ON t.trip_id=a.trip_id JOIN routes r ON r.route_id=t.route_id
        JOIN stops fs ON fs.stop_id=a.stop_id JOIN stops ts ON ts.stop_id=b.stop_id
        WHERE a.stop_id IN ({origin_marks}) AND b.stop_id IN ({dest_marks})
        AND a.departure_sec>=? AND a.departure_sec<=? {service_clause}
        ORDER BY a.departure_sec,b.arrival_sec LIMIT 300'''
    args = [*origin_ids,*destination_ids,target_sec,target_sec+4*3600,*service_args]
    direct_rows = con.execute(direct_sql,args).fetchall()
    itineraries=[]
    seen_routes=set()
    for row in direct_rows:
        if row['route_id'] in seen_routes:
            continue
        seen_routes.add(row['route_id'])
        itineraries.append({'transfers':0,'departure_sec':row['departure_sec'],'arrival_sec':row['arrival_sec'],'from_name':row['from_name'],'to_name':row['to_name'],'legs':[dict(row)]})
        if len(itineraries)>=8:
            break
    if itineraries:
        return itineraries

    dest_routes=[r['route_id'] for r in con.execute(
        f'''SELECT DISTINCT t.route_id FROM stop_times st JOIN trips t ON t.trip_id=st.trip_id
            WHERE st.stop_id IN ({dest_marks}) {service_clause}''', [*destination_ids,*service_args]).fetchall()]
    if not dest_routes:
        return []
    dest_route_marks=','.join('?' for _ in dest_routes)

    first_sql=f'''SELECT t.trip_id,t.route_id,t.service_id,t.headsign,t.shape_id,r.short_name,r.long_name,
        a.stop_id AS from_id,a.departure_sec AS departure_sec,b.stop_id AS transfer_id,b.arrival_sec AS transfer_arrival,
        a.stop_sequence AS from_seq,b.stop_sequence AS transfer_seq,fs.name AS from_name,xs.name AS transfer_name
        FROM stop_times a JOIN stop_times b ON b.trip_id=a.trip_id AND b.stop_sequence>a.stop_sequence
        JOIN trips t ON t.trip_id=a.trip_id JOIN routes r ON r.route_id=t.route_id JOIN stops fs ON fs.stop_id=a.stop_id JOIN stops xs ON xs.stop_id=b.stop_id
        WHERE a.stop_id IN ({origin_marks}) AND a.departure_sec>=? AND a.departure_sec<=? {service_clause}
        AND b.arrival_sec>a.departure_sec AND b.arrival_sec<=?
        AND b.stop_id IN (SELECT rs.stop_id FROM route_stops rs WHERE rs.route_id IN ({dest_route_marks}))
        ORDER BY b.arrival_sec LIMIT 1200'''
    first_args=[*origin_ids,target_sec,target_sec+3600,*service_args,target_sec+3*3600,*dest_routes]
    first_rows=con.execute(first_sql,first_args).fetchall()
    # Keep at most a few plausible board/transfer points to bound the second leg search.
    unique=[];seen=set()
    for row in first_rows:
        key=(row['trip_id'],row['transfer_id'])
        if key not in seen:
            seen.add(key);unique.append(dict(row))
        if len(unique)>=300: break
    for first in unique:
        second_marks=dest_marks
        second_sql=f'''SELECT t.trip_id,t.route_id,t.service_id,t.headsign,t.shape_id,r.short_name,r.long_name,
            a.stop_id AS from_id,a.departure_sec AS departure_sec,b.stop_id AS to_id,b.arrival_sec AS arrival_sec,
            a.stop_sequence AS from_seq,b.stop_sequence AS to_seq,fs.name AS from_name,ts.name AS to_name
            FROM stop_times a JOIN stop_times b ON b.trip_id=a.trip_id AND b.stop_sequence>a.stop_sequence
            JOIN trips t ON t.trip_id=a.trip_id JOIN routes r ON r.route_id=t.route_id
            JOIN stops fs ON fs.stop_id=a.stop_id JOIN stops ts ON ts.stop_id=b.stop_id
            WHERE a.stop_id=? AND b.stop_id IN ({second_marks}) AND a.departure_sec>=? AND a.departure_sec<=?
            AND t.route_id IN ({dest_route_marks}) AND t.route_id<>? {service_clause} ORDER BY b.arrival_sec LIMIT 4'''
        second_args=[first['transfer_id'],*destination_ids,first['transfer_arrival']+300,first['transfer_arrival']+7200,*dest_routes,first['route_id'],*service_args]
        for second in con.execute(second_sql,second_args).fetchall():
            total=second['arrival_sec']-first['departure_sec']
            if total<=0 or total>5*3600: continue
            first_leg=dict(first)
            first_leg['to_id']=first_leg['transfer_id']
            first_leg['to_name']=first_leg['transfer_name']
            first_leg['arrival_sec']=first_leg['transfer_arrival']
            itineraries.append({'transfers':1,'departure_sec':first['departure_sec'],'arrival_sec':second['arrival_sec'],
                'from_name':first['from_name'],'to_name':second['to_name'],'legs':[first_leg,dict(second)],'transfer_name':first['transfer_name']})
    result=[];seen_pairs=set()
    for itinerary in sorted(itineraries,key=lambda x:(x['arrival_sec']-x['departure_sec'],x['transfers'])):
        signature=tuple(leg['route_id'] for leg in itinerary['legs'])
        if signature in seen_pairs:
            continue
        seen_pairs.add(signature);result.append(itinerary)
        if len(result)>=8:break
    return result


_live_cache = {'expires': 0.0, 'timestamp': None, 'buses': [], 'error': None, 'source': None}
_live_lock = threading.Lock()
_eta_cache = {}
_eta_lock = threading.Lock()
_motion_state = {}
_motion_lock = threading.Lock()
_stop_grid = None
_stop_grid_lock = threading.Lock()


def stops_near(lat, lon, radius_m=35):
    """Check a small spatial grid so a planned stop dwell is not called traffic."""
    global _stop_grid
    if _stop_grid is None:
        with _stop_grid_lock:
            if _stop_grid is None:
                grid = {}
                con = gtfs_connection()
                if con:
                    for row in con.execute('SELECT lat,lon FROM stops WHERE lat IS NOT NULL AND lon IS NOT NULL'):
                        cell = (int(float(row['lat']) * 200), int(float(row['lon']) * 200))
                        grid.setdefault(cell, []).append((float(row['lat']), float(row['lon'])))
                    con.close()
                _stop_grid = grid
    lat_cell, lon_cell = int(lat * 200), int(lon * 200)
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for stop_lat, stop_lon in _stop_grid.get((lat_cell + dlat, lon_cell + dlon), ()):
                if geometry_distance_km([[lat, lon], [stop_lat, stop_lon]]) * 1000 <= radius_m:
                    return True
    return False


def annotate_live_motion(buses):
    """Flag repeated slow, stationary fresh positions away from mapped bus stops."""
    now = time.monotonic()
    with _motion_lock:
        for bus in buses:
            bus['traffic_status'] = 'unknown'
            if not bus.get('is_fresh'):
                continue
            speed = bus.get('speed_mps')
            vehicle_id = str(bus.get('vehicle_id') or '')
            if not vehicle_id or speed is None:
                continue
            lat, lon = float(bus['lat']), float(bus['lon'])
            previous = _motion_state.get(vehicle_id)
            moved_m = (geometry_distance_km([[lat, lon], [previous['lat'], previous['lon']]]) * 1000
                       if previous else 9999)
            if speed <= 0.8 and not stops_near(lat, lon):
                if previous and now - previous['last_seen'] <= 45 and moved_m <= 25:
                    slow_since = previous['slow_since']
                    samples = previous['samples'] + 1
                else:
                    slow_since, samples = now, 1
                held_seconds = int(now - slow_since)
                bus['traffic_status'] = 'possible_hold' if held_seconds >= 90 and samples >= 4 else 'slow'
                bus['held_seconds'] = held_seconds
                _motion_state[vehicle_id] = {
                    'lat': lat, 'lon': lon, 'last_seen': now,
                    'slow_since': slow_since, 'samples': samples,
                }
            else:
                bus['traffic_status'] = 'moving' if speed > 0.8 else 'near_stop'
                _motion_state[vehicle_id] = {
                    'lat': lat, 'lon': lon, 'last_seen': now,
                    'slow_since': now, 'samples': 0,
                }
        for vehicle_id, state in list(_motion_state.items()):
            if now - state['last_seen'] > 300:
                del _motion_state[vehicle_id]


def local_setting(name):
    value = os.getenv(name, '').strip()
    if value:
        return value
    try:
        for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == name:
                return value.strip().strip('"\'')
    except OSError:
        pass
    return ''


def live_vehicle_positions():
    """Fetch and decode OTD's authorized GTFS-Realtime vehicle feed."""
    dts_key = local_setting('DTS_GTFSRT_API_KEY')
    api_key = local_setting('OTD_API_KEY')
    if dts_key:
        query = urllib.parse.urlencode({'agency': 'delhi-buses', 'category': 'realtime_gtfs',
                                        'filename': 'VehiclePositions.pb'})
        feed_url = f'https://dts-backend.transportstack.in/api/dataset/otd/get-file?{query}'
        feed_headers = {'x-api-key': dts_key, 'User-Agent': 'DelhiKiPublic/0.4 (GTFS-Realtime consumer)'}
        feed_source = 'Delhi Transport Stack GTFS-Realtime'
    else:
        query = urllib.parse.urlencode({'key': api_key})
        feed_url = f'https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb?{query}'
        feed_headers = {'User-Agent': 'DelhiKiPublic/0.4 (GTFS-Realtime consumer)'}
        feed_source = 'Delhi OTD GTFS-Realtime'
    if not api_key and not dts_key:
        return {'available': False, 'configured': False, 'buses': [], 'note': 'Configure an authorized OTD or Transport Stack GTFS-Realtime API key.'}
    now = time.monotonic()
    with _live_lock:
        if _live_cache['expires'] > now:
            if _live_cache['error']:
                return {'available': False, 'configured': True, 'buses': [], 'note': _live_cache['error']}
            return {'available': True, 'configured': True, 'source': _live_cache['source'],
                    'timestamp': _live_cache['timestamp'], 'buses': _live_cache['buses']}
        try:
            from google.transit import gtfs_realtime_pb2
            request = urllib.request.Request(feed_url, headers=feed_headers)
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = response.read()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(payload)

            route_names = {}
            trip_routes = {}
            con = gtfs_connection()
            if con:
                route_names = {r['route_id']: r['short_name'] for r in con.execute('SELECT route_id,short_name FROM routes')}
                trip_routes = {r['trip_id']: r['route_id'] for r in con.execute('SELECT trip_id,route_id FROM trips')}
                con.close()

            timestamp = int(feed.header.timestamp) if feed.header.HasField('timestamp') else int(time.time())
            buses = []
            for entity in feed.entity:
                if not entity.HasField('vehicle'):
                    continue
                vehicle = entity.vehicle
                if not vehicle.HasField('position'):
                    continue
                position = vehicle.position
                if not (-90 <= position.latitude <= 90 and -180 <= position.longitude <= 180):
                    continue
                trip_id = vehicle.trip.trip_id if vehicle.HasField('trip') else ''
                route_id = vehicle.trip.route_id if vehicle.HasField('trip') else ''
                route_id = route_id or trip_routes.get(trip_id, '')
                vehicle_timestamp = int(vehicle.timestamp) if vehicle.HasField('timestamp') else None
                age_seconds = timestamp - vehicle_timestamp if vehicle_timestamp is not None else None
                buses.append({
                    'vehicle_id': vehicle.vehicle.id or entity.id,
                    'label': vehicle.vehicle.label or '',
                    'trip_id': trip_id,
                    'route_id': route_id,
                    'route': route_names.get(route_id, ''),
                    'lat': position.latitude,
                    'lon': position.longitude,
                    'bearing': position.bearing if position.HasField('bearing') else None,
                    'speed_mps': position.speed if position.HasField('speed') else None,
                    'timestamp': vehicle_timestamp,
                    'age_seconds': age_seconds,
                    'is_fresh': age_seconds is not None and -180 <= age_seconds <= 120,
                })
            annotate_live_motion(buses)
            _live_cache.update(expires=now + 15, timestamp=timestamp, buses=buses,
                               error=None, source=feed_source)
            return {'available': True, 'configured': True, 'source': feed_source,
                    'timestamp': timestamp, 'buses': buses}
        except ModuleNotFoundError:
            note = 'Install gtfs-realtime-bindings to decode live positions.'
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            note = f'Live feed is unavailable ({type(exc).__name__}). Check API access and network.'
        except Exception as exc:
            note = f'Could not decode the live feed ({type(exc).__name__}).'
        _live_cache.update(expires=now + 10, timestamp=None, buses=[], error=note, source=feed_source)
        return {'available': False, 'configured': True, 'buses': [], 'note': note}


def label(score):
    return {'level': 'low', 'en': 'Low crowd', 'hi': 'Kam bheed'} if score < 35 else {'level': 'medium', 'en': 'Medium crowd', 'hi': 'Thodi bheed'} if score < 68 else {'level': 'high', 'en': 'High crowd', 'hi': 'Bahut bheed'}


def profile_for(route, hour, day_type):
    if not PROFILE:
        return None
    route = str(route).upper()
    found = [PROFILE_INDEX[(route, int(hour), day_type)]] if (route, int(hour), day_type) in PROFILE_INDEX else []
    if not found:
        found = [p for p in PROFILE['profiles'] if p['route'].startswith(route) and p['hour'] == int(hour) and p['day_type'] == day_type]
    if found:
        return max(found, key=lambda p: p['ticket_records'])
    route_rows = [p for p in PROFILE['profiles'] if p['route'] == route and p['day_type'] == day_type]
    if not route_rows:
        route_rows = [p for p in PROFILE['profiles'] if p['route'].startswith(route) and p['day_type'] == day_type]
    if route_rows:
        closest = min(route_rows, key=lambda p: abs(p['hour'] - int(hour)))
        return {**closest, 'confidence': round(closest['confidence'] * .65, 2)}
    return None


def route_options(hour, day_type, origin, destination):
    if not PROFILE:
        return [], False
    o, d = origin.strip().casefold(), destination.strip().casefold()
    pairs = PROFILE.get('od_pairs', [])
    direct = [p for p in pairs if p['from'].strip().casefold() == o and p['to'].strip().casefold() == d]
    matched = bool(direct)
    if direct:
        # Prefer records observed in the requested day/time band; retain other
        # bands as a fallback because some direct stop pairs are sparse.
        band = [p for p in direct if p['hour'] == hour and p['day_type'] == day_type]
        candidates = band or direct
        by_route = {}
        for p in candidates:
            by_route[p['route']] = by_route.get(p['route'], 0) + p['ticket_records']
        ids = sorted(by_route, key=by_route.get, reverse=True)[:2]
    else:
        band = [p for p in PROFILE['profiles'] if p['hour'] == hour and p['day_type'] == day_type and p['ticket_records'] >= 10]
        if band:
            busy = max(band, key=lambda p: p['score'])['route']
            quiet = min(band, key=lambda p: p['score'])['route']
            ids = [busy, quiet] if busy != quiet else [busy]
        else:
            ids = [p['route'] for p in PROFILE.get('top_routes', [])[:2]]
    return ids, matched


def option(route, mins, transfers, score, confidence, walking=5, synthetic=True):
    return {
        'name': f'Route {route} · {"Direct bus" if transfers == 0 else "Bus transfer"}',
        'travel_time_min': mins, 'transfers': transfers, 'walking_min': walking,
        'crowd_score': int(score), 'crowd_label': label(score),
        'activity_summary': 'Lower sampled activity' if score < 35 else ('Mid-range sampled activity' if score < 68 else 'Higher sampled activity'),
        'confidence': confidence, 'synthetic': synthetic, 'legs': [
            {'mode': 'bus', 'route_id': str(route), 'crowd_score': int(score), 'crowd_label': label(score)}
        ], 'verdict': 'Fastest option' if mins < 50 else 'Less crowded option'
    }


@app.get('/', response_class=HTMLResponse)
def home():
    return (ROOT / 'frontend' / 'index.html').read_text(encoding='utf8')


@app.get('/app.js')
def frontend_script():
    return FileResponse(ROOT / 'frontend' / 'app.js', media_type='application/javascript')


@app.get('/healthz')
def healthcheck():
    return {'status': 'ok', 'ticket_profiles': bool(PROFILE),
            'gtfs_index': GTFS_FILE.exists(), 'gps_history': bool(GPS_PROFILE),
            'crowd_validation': bool(VALIDATION)}


@app.get('/data/status')
def data_status():
    if not PROFILE:
        return {'available': False, 'records': 0, 'routes': 0, 'gtfs_available': GTFS_FILE.exists(), 'gps_history_available': bool(GPS_PROFILE), 'source': 'Synthetic demo', 'note': 'Ticket archive not processed.'}
    status = {'available': True, 'records': PROFILE['records_processed'], 'routes': PROFILE['route_count'], 'gtfs_available': GTFS_FILE.exists(), 'gps_history_available': bool(GPS_PROFILE), 'gps_profile_count': len((GPS_PROFILE or {}).get('profiles', [])), 'source': PROFILE['source'], 'note': 'Ticket crowd scores are relative and are not passenger counts. Historical GPS speeds are a sampled baseline, not schedule-delay predictions. Static GTFS supplies schedules and shapes; live positions require a separate feed.'}
    if GPS_PROFILE:
        status['gps_history'] = {
            'source': GPS_PROFILE.get('source'),
            'files_processed': len(GPS_PROFILE.get('files_processed', [])),
            'routes': len({p['route_id'] for p in GPS_PROFILE.get('profiles', [])}),
            'limitations': GPS_PROFILE.get('limitations'),
        }
    con = gtfs_connection()
    if con:
        status['gtfs'] = {name: con.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('routes','stops','trips')}
        status['gtfs_source'] = 'Delhi Transport Stack static bus GTFS v32'
        status['accessibility'] = read_accessibility_coverage(con)
        con.close()
    return status


@app.get('/accessibility/status')
def accessibility_status():
    con = gtfs_connection()
    result = read_accessibility_coverage(con)
    if con:
        con.close()
    return result


@app.get('/feature-readiness')
def feature_readiness():
    """Report evidence-backed readiness for delay, routing and safety features."""
    con = gtfs_connection()
    accessibility = read_accessibility_coverage(con)
    if con:
        con.close()
    trip_updates = local_setting('OTD_TRIP_UPDATES_URL')
    service_alerts = local_setting('OTD_SERVICE_ALERTS_URL')
    pedestrian_key = local_setting('ORS_API_KEY')
    eta_key = local_setting('DTS_ETA_API_KEY')
    return {
        'live_vehicle_positions': {
            'configured': bool(local_setting('OTD_API_KEY') or local_setting('DTS_GTFSRT_API_KEY')),
            'purpose': 'Map display only; vehicle positions do not establish schedule delay.'
        },
        'traffic_hold_detection': {
            'configured': bool(local_setting('DTS_GTFSRT_API_KEY')),
            'purpose': 'Flags a possible hold after repeated fresh low-speed positions away from a mapped stop; not a confirmed delay.'
        },
        'delay_alerts': {
            'available': False, 'configured': bool(trip_updates and service_alerts),
            'trip_updates_configured': bool(trip_updates),
            'service_alerts_configured': bool(service_alerts),
            'purpose': 'Requires current GTFS-Realtime TripUpdates and ServiceAlerts feeds matched to this static GTFS.'
        },
        'bus_stop_etas': {
            'configured': bool(eta_key),
            'purpose': 'Transport Stack estimates arrivals for buses serving a GTFS stop; this is not a GTFS-RT disruption feed.'
        },
        'accessibility': accessibility,
        'pedestrian_routing': {
            'configured': bool(pedestrian_key),
            'purpose': 'Requires a pedestrian routing provider and usable stop/accessibility data.'
        },
        'women_safety': {
            'available': False,
            'purpose': 'No verified, sufficiently complete stop and street safety dataset is connected.'
        }
    }


def transportstack_eta(kind, identifier):
    api_key = local_setting('DTS_ETA_API_KEY')
    if not api_key:
        return {'available': False, 'configured': False,
                'note': 'Transport Stack ETA access requires approval and the API key sent by email.'}
    if kind == 'stop':
        endpoint, param = 'stop/etas', 'stop_id'
    else:
        endpoint, param = 'bus/etas', 'vehicle_id'
    cache_key = (kind, str(identifier))
    now = time.monotonic()
    with _eta_lock:
        cached = _eta_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        query = urllib.parse.urlencode({param: str(identifier)})
        request = urllib.request.Request(
            f'https://dts-backend.transportstack.in/api/serviceset/eta-calculator/{endpoint}?{query}',
            headers={'x-api-key': api_key, 'Accept': 'application/json',
                     'User-Agent': 'DelhiKiPublic/0.4'},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('Unexpected ETA response')
            result = {**payload, 'available': True, 'configured': True,
                      'source': 'Delhi Transport Stack ETA Calculator'}
            _eta_cache[cache_key] = (now + 15, result)
            return result
        except urllib.error.HTTPError as exc:
            note = 'ETA access was denied; check approval and the API key.' if exc.code in (401, 403) else f'ETA service returned HTTP {exc.code}.'
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            note = 'Transport Stack ETA service is temporarily unavailable.'
        result = {'available': False, 'configured': True, 'note': note}
        _eta_cache[cache_key] = (now + 8, result)
        return result


@app.get('/eta/stop/{stop_id}')
def stop_etas(stop_id: str):
    return transportstack_eta('stop', stop_id)


@app.get('/eta/bus/{vehicle_id}')
def bus_eta(vehicle_id: str):
    return transportstack_eta('bus', vehicle_id)


@app.get('/gps/route/{route_id}')
def gps_route(route_id: str, day_type: str = Query('weekday', pattern='^(weekday|weekend)$'), hour: int = Query(8, ge=0, le=23)):
    profile = gps_profile_for(route_id, hour, day_type)
    if not profile:
        return {'available': False, 'route_id': route_id, 'note': 'No historical GPS speed profile is loaded for this route and day type.'}
    return {
        'available': True, 'route_id': route_id, 'hour': profile['hour'],
        'day_type': profile['day_type'], 'median_speed_kmh': round(profile['median_speed_mps'] * 3.6, 1),
        'mean_speed_kmh': round(profile['mean_speed_mps'] * 3.6, 1),
        'observations': profile['observations'], 'hour_matched': profile.get('hour_matched', True),
        'source': (GPS_PROFILE or {}).get('source'),
        'note': 'Historical sampled bus speed; it is not a live position or a schedule-delay prediction.',
    }


@app.get('/accuracy')
def accuracy():
    if not VALIDATION:
        return {'available': False, 'note': 'Run make validation to build the chronological ticket holdout report.'}
    return {'available': True, **VALIDATION}


@app.get('/stops')
def stops(q: str = ''):
    con = gtfs_connection()
    if con:
        matches = stop_matches(con, q, 20) if q else [dict(r) for r in con.execute('SELECT stop_id,code AS stop_code,name,lat,lon FROM stops ORDER BY name LIMIT 20')]
        con.close()
        return {'stops': [m['name'] for m in matches], 'locations': matches}
    if not PROFILE:
        return {'stops': [], 'locations': []}
    q = q.strip().casefold()
    matches = [s for s in PROFILE.get('stops', []) if q in s.casefold()] if q else PROFILE.get('stops', [])
    return {'stops': matches[:20], 'locations': []}


@app.get('/plan')
def plan(from_: str = Query('', alias='from'), to: str = '', time: str = '08:30', date_: str = Query('', alias='date'), comfort: int = 50, friendly: bool = False, special: bool = False, travel_profile: str = Query('standard', pattern='^(standard|comfort|family|older_adult|wheelchair|women_safety)$')):
    try:
        hour = int(time.split(':')[0])
    except (ValueError, IndexError):
        hour = 8
    try:
        chosen = date.fromisoformat(date_) if date_ else date.today()
    except ValueError:
        chosen = date.today()
    day_type = 'weekend' if chosen.weekday() >= 5 else 'weekday'
    try:
        hh, mm = (int(x) for x in time.split(':')[:2])
        target_sec = hh * 3600 + mm * 60
    except (ValueError, AttributeError):
        target_sec = 8 * 3600 + 30 * 60
    con = gtfs_connection()
    itinerary_rows = []
    origin_stops, destination_stops = [], []
    if con:
        origin_ids, origin_stops = stop_lookup(con, from_ or 'Kashmere Gate')
        destination_ids, destination_stops = stop_lookup(con, to or 'Connaught Place')
        services = active_services(con, chosen)
        itinerary_rows = find_itineraries(con, origin_ids, destination_ids, target_sec, services)
        for itinerary in itinerary_rows:
            itinerary['wheelchair_accessibility'] = itinerary_wheelchair_access(con, itinerary)
        if travel_profile == 'wheelchair':
            itinerary_rows = [itinerary for itinerary in itinerary_rows
                              if itinerary['wheelchair_accessibility']['status'] == 'verified_accessible']
    rows=[]
    for itinerary in itinerary_rows[:6]:
        legs=[]; weighted_score=0; weighted_confidence=0; covered=0; record_total=0; duration_total=0
        for leg_index, raw in enumerate(itinerary['legs']):
            route_label = raw.get('short_name') or raw.get('long_name') or str(raw['route_id'])
            duration = max(0, int(raw['arrival_sec']) - int(raw['departure_sec']))
            profile = profile_for(route_label, hour, day_type)
            score = profile['score'] if profile else 50
            record_total += profile['ticket_records'] if profile else 0
            if profile:
                weighted_score += score * max(duration,1)
                weighted_confidence += profile['confidence'] * max(duration,1)
                duration_total += max(duration,1)
                covered += 1
            geometry = leg_geometry(con, raw['trip_id'], raw['from_id'], raw['to_id'], raw.get('shape_id'))
            gps_profile = gps_profile_for(raw['route_id'], hour, day_type)
            gps_distance = geometry_distance_km(geometry) if len(geometry) >= 2 else 0
            gps_minutes = (gps_distance / (gps_profile['median_speed_mps'] * 3.6) * 60
                           if gps_profile and gps_profile['median_speed_mps'] > 0 and gps_distance > 0 else None)
            wheelchair = itinerary.get('wheelchair_accessibility', {}).get('legs', [])
            wheelchair = wheelchair[leg_index] if leg_index < len(wheelchair) else {'status': 'unknown'}
            legs.append({
                'mode':'bus','route_id':raw['route_id'],'route_short_name':route_label,
                'headsign':raw.get('headsign') or '', 'from_stop_id':raw['from_id'],'to_stop_id':raw['to_id'],
                'from_stop':raw['from_name'],'to_stop':raw['to_name'],
                'departure_sec':int(raw['departure_sec']),'arrival_sec':int(raw['arrival_sec']),
                'crowd_score':score if profile else None,'crowd_label':label(score) if profile else {'level':'unknown','en':'No ticket profile','hi':'टिकट डेटा नहीं'},
                'confidence':profile['confidence'] if profile else 0,'ticket_records':profile['ticket_records'] if profile else 0,
                'profile_hour':profile['hour'] if profile else None,'geometry':geometry,
                'historical_speed_kmh':round(gps_profile['median_speed_mps'] * 3.6, 1) if gps_profile else None,
                'historical_speed_observations':gps_profile['observations'] if gps_profile else 0,
                'historical_speed_hour':gps_profile.get('hour', hour) if gps_profile else None,
                'historical_speed_hour_matched':gps_profile.get('hour_matched', True) if gps_profile else False,
                'gps_speed_based_duration_min':round(gps_minutes) if gps_minutes is not None else None,
                'wheelchair_accessibility':wheelchair,
            })
        mean = round(weighted_score / duration_total) if duration_total else 50
        crowd_available = covered == len(legs) and covered > 0
        crowd_txt = 'Lower sampled activity' if mean < 35 else 'Mid-range sampled activity' if mean < 68 else 'Higher sampled activity'
        if not crowd_available:
            crowd_txt = 'Ticket profile unavailable'
        route_labels=[leg['route_short_name'] for leg in legs]
        wait = max(0, (legs[1]['departure_sec'] - legs[0]['arrival_sec']) // 60) if len(legs)>1 else 0
        rows.append({
            'name':f"Route {' + '.join(route_labels)} · {'direct' if not itinerary['transfers'] else '1 transfer'}",
            'travel_time_min':max(1,round((itinerary['arrival_sec']-itinerary['departure_sec'])/60)),
            'transfers':itinerary['transfers'],'walking_min':0,'wait_min':wait,
            'crowd_score':mean,'crowd_label':label(mean),'activity_summary':crowd_txt,
            'crowd_data_backed':crowd_available,'confidence':round(weighted_confidence/duration_total,2) if duration_total else .18,
            'synthetic':False,'ticket_records':record_total,'legs':legs,
            'wheelchair_accessibility':itinerary.get('wheelchair_accessibility',{}).get('status','unknown'),
            'verdict':'Fastest scheduled option' if len(rows)==0 else 'Alternative scheduled option'
        })
    if friendly or travel_profile in ('comfort','family','older_adult'):
        lower_crowd = [row for row in rows if not row['crowd_data_backed'] or row['crowd_score'] < 68]
        direct = [row for row in lower_crowd if row['transfers'] == 0]
        rows = direct or lower_crowd or [row for row in rows if row['transfers'] == 0] or rows
        if travel_profile in ('family','older_adult'):
            rows.sort(key=lambda row: (row['transfers'], row['wait_min'], row['crowd_score'] if row['crowd_data_backed'] else 50, row['travel_time_min']))
    coverage = read_accessibility_coverage(con)
    profile_notice = None
    if travel_profile in ('comfort', 'family', 'older_adult'):
        profile_notice = 'This preference favors fewer transfers and lower sampled crowd. Walking distance and safety are not assessed with the available feeds.'
    elif travel_profile == 'wheelchair':
        profile_notice = 'Only routes with explicit accessible bus, boarding stops, and destination stops are shown. Unknown data is not treated as accessible.'
        if not coverage['available']:
            profile_notice = 'Wheelchair-accessible routes cannot be verified because the current bus GTFS lacks complete wheelchair fields. No route is labelled accessible.'
    elif travel_profile == 'women_safety':
        profile_notice = 'Verified safety information for stops and walking paths is not connected. These routes are not ranked as safer.'
    tip_time = None
    tip_text = 'No quieter departure is clear in the ticket sample for this day type.'
    if PROFILE:
        hours = [p for p in PROFILE.get('hourly', []) if p['day_type'] == day_type]
        current = next((p for p in hours if int(p['hour']) == hour), None)
        quieter = [p for p in hours if current and 6 <= int(p['hour']) <= 20 and
                   abs(int(p['hour']) - hour) <= 3 and int(p['hour']) != hour and
                   p['score'] <= current['score'] - 8]
        if quieter:
            suggested = min(quieter, key=lambda p: (abs(int(p['hour']) - hour), p['score']))
            suggested_hour = int(suggested['hour'])
            tip_time = f'{suggested_hour:02d}:30'
            tip_text = (f"The city-wide ticket sample shows lower relative activity around {suggested_hour}:00 "+
                        f"({suggested['score']}/100) than at {hour}:00 ({current['score']}/100). " +
                        'Crowd data is hourly; check the scheduled buses after applying the time.')
    anomalies = (VALIDATION or {}).get('daily_ticket_anomalies', [])
    selected_anomaly = next((x for x in anomalies if x['date'] == str(chosen)), None) if special else None
    if selected_anomaly:
        direction = 'above' if selected_anomaly['direction'] == 'high' else 'below'
        special_notice = {
            'active': True, 'source': 'historical_ticket_volume',
            'message': (f"The October ticket sample for this date was {abs(selected_anomaly['deviation_percent']):g}% {direction} its "+
                        'weekday/weekend median. This is a sample-volume anomaly; its cause is unknown and crowd scores are not adjusted.'),
        }
    elif special:
        special_notice = {
            'active': False, 'source': 'historical_ticket_volume',
            'message': 'No matching unusual ticket-volume day is available for this date; no crowd-score adjustment was made.',
        }
    else:
        special_notice = None
    return {
        'from': from_ or 'Kashmere Gate', 'to': to or 'Connaught Place', 'date': str(chosen),
        'day_type': day_type, 'mode': 'bus', 'comfort': comfort, 'travel_profile': travel_profile,
        'relative_crowding_notice': 'Crowd scores rank October ticket activity matched to GTFS route names. Tickets cover a sample and are not passenger counts.',
        'options': rows, 'leave_tip': {'time': tip_time, 'text': tip_text, 'synthetic': False},
        'nearby_stop': None,
        'profile_notice': profile_notice,
        'accessibility': coverage,
        'special_day': special_notice,
        'data_backed': bool(PROFILE),'gtfs_backed':bool(con),'direct_pair_found':any(x['transfers']==0 for x in itinerary_rows),
        'itinerary_notice': 'Scheduled bus routes, stops, and shape geometry come from static GTFS. Times are scheduled and not traffic-adjusted.' if itinerary_rows else ('No matching scheduled bus itinerary was found for these stops, date, and departure time. Try a different stop suggestion or time.' if con else 'Static GTFS is not indexed; route path is illustrative.'),
        'synthetic': not bool(itinerary_rows)
    }


@app.get('/crowd/route/{id}')
def crowd_route(id: str, day_type: str = 'weekday', hour: int = 8):
    p = profile_for(id, hour, day_type)
    score = p['score'] if p else 50
    return {'route_id': id, 'day_type': day_type, 'hour': hour, 'crowd_score': score, 'crowd_label': label(score), 'confidence': p['confidence'] if p else .2, 'ticket_records': p['ticket_records'] if p else 0, 'synthetic': p is None}


@app.get('/crowd/segments/{route_id}')
def segments(route_id: str, day_type: str = 'weekday', hour: int = 8):
    if not PROFILE:
        return {'route_id': route_id, 'segments': [], 'available': False, 'synthetic': True,
                'note': 'Ticket profile has not been built.'}
    wanted = route_id.strip().upper()
    pairs = PROFILE.get('od_pairs', [])
    matches = [p for p in pairs if str(p.get('route', '')).upper() == wanted]
    if not matches:
        matches = [p for p in pairs if str(p.get('route', '')).upper().startswith(wanted)]
    selected = [p for p in matches if p.get('day_type') == day_type and int(p.get('hour', -1)) == int(hour)]
    period_match = bool(selected)
    if not selected:
        selected = [p for p in matches if p.get('day_type') == day_type]
    if not selected:
        selected = matches
    grouped = {}
    for row in selected:
        key = (str(row.get('from', '')).strip(), str(row.get('to', '')).strip())
        if not all(key):
            continue
        acc = grouped.setdefault(key, {'ticket_records': 0, 'passenger_proxy': 0.0})
        acc['ticket_records'] += int(row.get('ticket_records', 0) or 0)
        acc['passenger_proxy'] += float(row.get('passenger_proxy', 0) or 0)
    ranked = sorted(grouped.items(), key=lambda item: item[1]['ticket_records'], reverse=True)
    count = len(ranked)
    result = []
    for index, ((from_stop, to_stop), values) in enumerate(ranked[:12]):
        score = 50 if count <= 1 else round(100 * (count - index - 1) / (count - 1))
        result.append({
            'from_stop': from_stop,
            'to_stop': to_stop,
            'ticket_records': values['ticket_records'],
            'passenger_proxy': round(values['passenger_proxy'], 1),
            'relative_activity_score': score,
            'relative_activity_label': 'Lower sampled activity' if score < 35 else 'Mid-range sampled activity' if score < 68 else 'Higher sampled activity',
        })
    return {
        'route_id': route_id,
        'segments': result,
        'available': bool(result),
        'day_type': day_type,
        'hour': int(hour),
        'period_matched': period_match,
        'note': 'Ticket-derived origin–destination activity within the route sample. These records do not measure onboard occupancy or seat availability.',
        'synthetic': False,
    }


@app.get('/crowd/heatmap')
def heatmap(hour: int = 8, day_type: str = 'weekday'):
    if not PROFILE:
        return {'hour': hour, 'day_type': day_type, 'points': [], 'synthetic': True}
    routes = sorted((p for p in PROFILE['profiles'] if p['hour'] == hour and p['day_type'] == day_type), key=lambda p: p['score'], reverse=True)[:8]
    con = gtfs_connection()
    shapes = []
    if con:
        for profile in routes:
            route = con.execute('SELECT route_id FROM routes WHERE upper(short_name)=?', (profile['route'].upper(),)).fetchone()
            if not route:
                continue
            shape = con.execute('''SELECT t.shape_id,COUNT(*) AS points FROM trips t JOIN shapes s USING(shape_id)
                WHERE t.route_id=? AND t.shape_id<>'' GROUP BY t.shape_id ORDER BY points DESC LIMIT 1''', (route['route_id'],)).fetchone()
            if not shape:
                continue
            geometry = [[float(r['lat']), float(r['lon'])] for r in con.execute(
                'SELECT lat,lon FROM shapes WHERE shape_id=? ORDER BY point_sequence', (shape['shape_id'],))]
            if len(geometry) >= 2:
                shapes.append({'route':profile['route'], 'route_id':route['route_id'],
                               'score':profile['score'], 'ticket_records':profile['ticket_records'],
                               'geometry':geometry})
        con.close()
    return {'hour': hour, 'day_type': day_type, 'points': [], 'route_profiles': routes,
            'route_shapes': shapes, 'synthetic': False,
            'note': 'Relative ticket activity mapped to representative GTFS route shapes; these are not passenger-density or occupancy measurements.'}


@app.get('/crowd/trends')
def crowd_trends(day_type: str = 'weekday'):
    if not PROFILE:
        return {'day_type': day_type, 'hours': [], 'synthetic': True}
    return {'day_type': day_type, 'hours': [p for p in PROFILE['hourly'] if p['day_type'] == day_type], 'synthetic': False}


@app.get('/live/route/{id}')
def live(id: str):
    feed = live_vehicle_positions()
    if not feed['available']:
        return {'route_id': id, **feed, 'synthetic': True}
    requested = id.strip().casefold()
    route_buses = [b for b in feed['buses'] if b['route_id'].casefold() == requested or b['route'].casefold().startswith(requested)]
    buses = [b for b in route_buses if b.get('is_fresh')]
    traffic_holds = [b for b in buses if b.get('traffic_status') == 'possible_hold']
    return {
        'route_id': id,
        'available': True,
        'configured': True,
        'timestamp': feed['timestamp'],
        'buses': buses,
        'count': len(buses),
        'stale_positions_ignored': len(route_buses) - len(buses),
        'traffic_holds': traffic_holds,
        'traffic_notice': 'Possible traffic hold means a fresh bus position stayed nearly stationary below 0.8 m/s for at least 90 seconds away from a mapped GTFS stop. It is not a confirmed schedule delay.',
        'source': feed.get('source'),
        'note': 'Fresh live vehicle positions from the authorized Delhi GTFS-Realtime feed.',
        'synthetic': False,
    }


@app.get('/insights')
def insights():
    if PROFILE:
        peak = [p for p in PROFILE['profiles'] if p['hour'] in (8, 9) and p['day_type'] == 'weekday']
        by_route = {}
        for p in peak:
            row = by_route.setdefault(p['route'], {'route': p['route'], 'scores': [], 'records': 0})
            row['scores'].append(p['score'])
            row['records'] += p['ticket_records']
        morning = [{**r, 'score': round(sum(r['scores']) / len(r['scores']))} for r in by_route.values()]
        top = sorted(morning, key=lambda p: p['score'], reverse=True)[:6]
        quiet = sorted(morning, key=lambda p: p['score'])[:4]
        return {
            'overcrowded_routes': [{'route': p['route'], 'score': p['score'], 'records': p['records']} for p in top],
            'worst_segments': [],
            'hours_needing_buses': [],
            'underused_routes': [{'route': p['route'], 'score': p['score']} for p in quiet],
            'synthetic': False, 'source': PROFILE['source'], 'records_processed': PROFILE['records_processed']
        }
    return {'overcrowded_routes': [], 'worst_segments': [], 'hours_needing_buses': [], 'underused_routes': [], 'synthetic': True}
