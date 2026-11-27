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
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title='Delhi Ki Public API', description='Delhi Ki Public — Know the crowd before you board. Crowd scores are relative estimates, not passenger counts.', version='0.2.0')
ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / 'data' / 'processed' / 'crowd_profiles.json'
GTFS_FILE = ROOT / 'data' / 'processed' / 'gtfs.sqlite'
try:
    PROFILE = json.loads(PROFILE_FILE.read_text(encoding='utf-8'))
except (OSError, json.JSONDecodeError):
    PROFILE = None
PROFILE_INDEX = {
    (p['route'], p['hour'], p['day_type']): p
    for p in (PROFILE or {}).get('profiles', [])
}


def gtfs_connection():
    if not GTFS_FILE.exists():
        return None
    con = sqlite3.connect(f'file:{GTFS_FILE}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


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


class Alert(BaseModel):
    origin: str
    destination: str
    departure: str = '08:30'
    day_type: str = 'weekday'
    enabled: bool = True


alerts = []
_live_cache = {'expires': 0.0, 'timestamp': None, 'buses': [], 'error': None}
_live_lock = threading.Lock()


def live_vehicle_positions():
    """Fetch and decode OTD's authorized GTFS-Realtime vehicle feed."""
    api_key = os.getenv('OTD_API_KEY', '').strip()
    if not api_key:
        env_file = ROOT / '.env'
        try:
            for line in env_file.read_text(encoding='utf-8').splitlines():
                key, separator, value = line.partition('=')
                if separator and key.strip() == 'OTD_API_KEY':
                    api_key = value.strip().strip('"\'')
                    break
        except OSError:
            pass
    if not api_key:
        return {'available': False, 'configured': False, 'buses': [], 'note': 'Set OTD_API_KEY to enable live bus positions.'}
    now = time.monotonic()
    with _live_lock:
        if _live_cache['expires'] > now:
            if _live_cache['error']:
                return {'available': False, 'configured': True, 'buses': [], 'note': _live_cache['error']}
            return {'available': True, 'configured': True, 'timestamp': _live_cache['timestamp'], 'buses': _live_cache['buses']}
        try:
            from google.transit import gtfs_realtime_pb2
            query = urllib.parse.urlencode({'key': api_key})
            request = urllib.request.Request(
                f'https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb?{query}',
                headers={'User-Agent': 'DelhiKiPublic/0.3 (GTFS-Realtime consumer)'},
            )
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
                    'timestamp': int(vehicle.timestamp) if vehicle.HasField('timestamp') else None,
                })
            timestamp = int(feed.header.timestamp) if feed.header.HasField('timestamp') else None
            _live_cache.update(expires=now + 15, timestamp=timestamp, buses=buses, error=None)
            return {'available': True, 'configured': True, 'timestamp': timestamp, 'buses': buses}
        except ModuleNotFoundError:
            note = 'Install gtfs-realtime-bindings to decode live positions.'
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            note = f'Live feed is unavailable ({type(exc).__name__}). Check API access and network.'
        except Exception as exc:
            note = f'Could not decode the live feed ({type(exc).__name__}).'
        _live_cache.update(expires=now + 10, timestamp=None, buses=[], error=note)
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
        'name': f'Route {route} · {"Direct bus" if transfers == 0 else "Bus + Metro"}',
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


@app.get('/data/status')
def data_status():
    if not PROFILE:
        return {'available': False, 'records': 0, 'routes': 0, 'gtfs_available': GTFS_FILE.exists(), 'source': 'Synthetic demo', 'note': 'Ticket archive not processed.'}
    status = {'available': True, 'records': PROFILE['records_processed'], 'routes': PROFILE['route_count'], 'gtfs_available': GTFS_FILE.exists(), 'source': PROFILE['source'], 'note': 'Ticket crowd scores are relative and are not passenger counts. Static GTFS supplies bus stops, schedules and shapes; metro and live GPS feeds are still unconnected.'}
    con = gtfs_connection()
    if con:
        status['gtfs'] = {name: con.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('routes','stops','trips')}
        status['gtfs_source'] = 'Delhi Transport Stack static bus GTFS v32'
        con.close()
    return status


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
def plan(from_: str = Query('', alias='from'), to: str = '', time: str = '08:30', date_: str = Query('', alias='date'), mode: str = 'bus_metro', comfort: int = 50, friendly: bool = False, special: bool = False):
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
    rows=[]
    for itinerary in itinerary_rows[:6]:
        legs=[]; weighted_score=0; weighted_confidence=0; covered=0; record_total=0; duration_total=0
        for raw in itinerary['legs']:
            route_label = raw.get('short_name') or raw.get('long_name') or str(raw['route_id'])
            duration = max(0, int(raw['arrival_sec']) - int(raw['departure_sec']))
            profile = profile_for(route_label, hour, day_type)
            score = profile['score'] if profile else 50
            if special and profile:
                score = min(100, score + 8)
            record_total += profile['ticket_records'] if profile else 0
            if profile:
                weighted_score += score * max(duration,1)
                weighted_confidence += profile['confidence'] * max(duration,1)
                duration_total += max(duration,1)
                covered += 1
            geometry = leg_geometry(con, raw['trip_id'], raw['from_id'], raw['to_id'], raw.get('shape_id'))
            legs.append({
                'mode':'bus','route_id':raw['route_id'],'route_short_name':route_label,
                'headsign':raw.get('headsign') or '', 'from_stop_id':raw['from_id'],'to_stop_id':raw['to_id'],
                'from_stop':raw['from_name'],'to_stop':raw['to_name'],
                'departure_sec':int(raw['departure_sec']),'arrival_sec':int(raw['arrival_sec']),
                'crowd_score':score if profile else None,'crowd_label':label(score) if profile else {'level':'unknown','en':'No ticket profile','hi':'टिकट डेटा नहीं'},
                'confidence':profile['confidence'] if profile else 0,'ticket_records':profile['ticket_records'] if profile else 0,
                'profile_hour':profile['hour'] if profile else None,'geometry':geometry
            })
        mean = round(weighted_score / duration_total) if duration_total else 50
        if special and not covered:
            mean = 50
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
            'verdict':'Fastest scheduled option' if len(rows)==0 else 'Alternative scheduled option'
        })
    if friendly:
        direct = [row for row in rows if row['transfers']==0]
        rows = direct or rows
    tip_time = '11:30' if hour < 10 else '14:30' if hour < 16 else '17:30'
    return {
        'from': from_ or 'Kashmere Gate', 'to': to or 'Connaught Place', 'date': str(chosen),
        'day_type': day_type, 'mode': mode, 'comfort': comfort,
        'relative_crowding_notice': 'Crowd scores rank October ticket activity matched to GTFS route names. Tickets cover a sample and are not passenger counts.',
        'options': rows, 'leave_tip': {'time': tip_time, 'text': f'Try {tip_time}: compare the scheduled bus options and ticket crowd profile.', 'synthetic': False},
        'nearby_stop': None,
        'special_day': {'active': True, 'message': 'Illustrative what-if adjustment: +8 crowd points. No special-event history is connected.'} if special else None,
        'data_backed': bool(PROFILE),'gtfs_backed':bool(con),'direct_pair_found':any(x['transfers']==0 for x in itinerary_rows),
        'itinerary_notice': ('Scheduled bus routes, stops, and shape geometry come from static GTFS. Times are scheduled and not traffic-adjusted.' + (' The current preference is bus + metro, but metro routing data is not connected; these options are bus-only.' if mode=='bus_metro' else '')) if itinerary_rows else ('No matching scheduled bus itinerary was found for these stops, date, and departure time. Try a different stop suggestion or time.' if con else 'Static GTFS is not indexed; route path is illustrative.'),
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
    return {'hour': hour, 'day_type': day_type, 'points': [], 'route_profiles': routes, 'synthetic': False, 'note': 'Ticket data has no stop coordinates; geographic heatmap awaits GTFS stop locations.'}


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
    buses = [b for b in feed['buses'] if b['route_id'].casefold() == requested or b['route'].casefold().startswith(requested)]
    return {
        'route_id': id,
        'available': True,
        'configured': True,
        'timestamp': feed['timestamp'],
        'buses': buses,
        'count': len(buses),
        'note': 'Live vehicle positions from the authorized Delhi OTD GTFS-Realtime feed.',
        'synthetic': False,
    }


@app.get('/alerts')
def get_alerts():
    return alerts


@app.post('/alerts')
def post_alert(a: Alert):
    alerts.append(a.model_dump())
    return {'saved': True, 'alert': a}


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
