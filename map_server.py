#!/usr/bin/env python3
"""
Whale sightings map server.

Serves a MapLibre map page, proxies the maplify.com sightings API,
and streams live vessel positions from AISstream.io via a background
WebSocket thread.

Usage:
    pip install flask requests websocket-client
    python3 map_server.py                  # http://localhost:5000
    python3 map_server.py --port 8080

AIS vessel layer:
    Set AIS_API_KEY env var to your key from https://aisstream.io/
    Without a key the /api/vessels endpoint returns an empty list
    and the map simply shows no vessel layer.

    export AIS_API_KEY=your_key_here
    python3 map_server.py
"""

import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import websocket
from flask import Flask, jsonify, request, send_from_directory

# Load .env from the same directory as this script (dev/.env)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIGHTINGS_URL = 'https://maplify.com/waseak/php/search-all-sightings.php'
AIS_WS_URL    = 'wss://stream.aisstream.io/v0/stream'
REQUEST_TIMEOUT = 30
DEFAULT_DAYS    = 7

# AIS bounding boxes — each entry is [[min_lat, min_lon], [max_lat, max_lon]]
AIS_BBOX = [
    [[37.2, -123.5], [38.8, -121.5]],   # SF Bay + Half Moon Bay + outer approaches
    [[36.3, -122.5], [37.2, -121.7]],   # Monterey Bay
    [[47.0, -123.2], [48.8, -122.0]],   # Puget Sound (Olympia → Bellingham)
    [[47.9, -124.9], [48.8, -122.4]],   # Strait of Juan de Fuca
    [[34.2, -120.5], [34.6, -119.4]],   # Santa Barbara Channel
    [[34.1, -119.5], [34.4, -119.0]],   # Ventura Harbor
    [[35.2, -121.0], [35.5, -120.7]],   # Morro Bay
    [[46.0, -124.7], [46.8, -123.5]],   # Columbia River mouth (Astoria)
    [[33.8, -120.8], [34.5, -118.8]],   # Channel Islands / Santa Barbara Channel
    [[33.4, -118.7], [33.9, -117.8]],   # Long Beach / San Pedro Bay
    [[32.4, -117.5], [32.9, -117.0]],   # San Diego Bay + approaches
]

# Drop vessels not heard from in this many minutes
VESSEL_TTL_MIN  = 30
TRACK_MAX_PTS   = 300   # ~1-3 hours depending on vessel update rate

AIS_API_KEY = os.environ.get('AIS_API_KEY', '')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Ship type category mapping (AIS type codes → display category)
# ---------------------------------------------------------------------------
def _ship_category(type_code):
    t = int(type_code or 0)
    if 70 <= t <= 79: return 'cargo',     'Cargo'
    if 80 <= t <= 89: return 'tanker',    'Tanker'
    if 60 <= t <= 69: return 'passenger', 'Passenger'
    if 40 <= t <= 49: return 'hsc',       'High Speed Craft'
    if t == 30:       return 'fishing',   'Fishing'
    if t in (31, 32): return 'tug',       'Towing'
    if t == 52:       return 'tug',       'Tug'
    if t == 36:       return 'sailing',   'Sailing'
    if t == 37:       return 'pleasure',  'Pleasure Craft'
    if t in (50, 51, 53, 54, 55, 58): return 'special', 'Special'
    return 'unknown', 'Unknown'


# ---------------------------------------------------------------------------
# Vessel store  (keyed by MMSI string)
# ---------------------------------------------------------------------------
_vessels      = {}   # mmsi → {mmsi, name, lat, lon, sog, cog, updated_at}
_vessel_static = {}  # mmsi → {category, type_label, length, beam, draught, callsign, imo, destination}
_tracks       = {}   # mmsi → deque of [lon, lat] in GeoJSON order
_vessels_lock = threading.Lock()
_sample_msg        = {}   # last raw position message seen, for debugging
_sample_static_msg = {}   # last StaticDataReport seen, for debugging


def _update_vessel(mmsi, name, lat, lon, sog, cog, heading, ship_type):
    mmsi_str = str(mmsi)
    lat = round(float(lat), 5)
    lon = round(float(lon), 5)
    with _vessels_lock:
        _vessels[mmsi_str] = {
            'mmsi':       mmsi_str,
            'name':       (name or '').strip() or f'MMSI {mmsi}',
            'lat':        lat,
            'lon':        lon,
            'sog':        round(float(sog or 0), 1),
            'cog':        round(float(cog or 0), 1),
            'heading':    heading,
            'type':       int(ship_type or 0),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        # Append to track only when the vessel has meaningfully moved (>~10 m)
        track = _tracks.setdefault(mmsi_str, deque(maxlen=TRACK_MAX_PTS))
        if not track or abs(lat - track[-1][1]) > 0.0001 or abs(lon - track[-1][0]) > 0.0001:
            track.append([lon, lat])  # GeoJSON coord order


def _expire_vessels():
    while True:
        time.sleep(300)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=VESSEL_TTL_MIN)
        with _vessels_lock:
            stale = [m for m, v in _vessels.items()
                     if datetime.fromisoformat(v['updated_at']) < cutoff]
            for m in stale:
                del _vessels[m]
        for m in stale:
            _tracks.pop(m, None)
            _vessel_static.pop(m, None)
        if stale:
            log.info(f'AIS: expired {len(stale)} stale vessel(s)')


# ---------------------------------------------------------------------------
# AIS WebSocket background thread
# ---------------------------------------------------------------------------
def _process_ais_message(raw):
    try:
        data     = json.loads(raw)
        msg_type = data.get('MessageType', '')
        msg      = data.get('Message', {})
        meta     = data.get('MetaData', {})

        if msg_type == 'ShipStaticData':
            inner = msg.get('ShipStaticData', {})
            mmsi  = str(meta.get('MMSI') or inner.get('UserID') or '')
            if not mmsi:
                return
            type_code        = inner.get('Type', 0)
            category, label  = _ship_category(type_code)
            bow   = inner.get('DimensionToBow',        0) or 0
            stern = inner.get('DimensionToStern',      0) or 0
            port  = inner.get('DimensionToPort',       0) or 0
            stbd  = inner.get('DimensionToStarboard',  0) or 0
            with _vessels_lock:
                _vessel_static[mmsi] = {
                    'category':    category,
                    'type_label':  label,
                    'length':      bow + stern,
                    'beam':        port + stbd,
                    'draught':     round(float(inner.get('MaximumStaticDraught') or 0), 1),
                    'callsign':    (inner.get('CallSign',    '') or '').strip(),
                    'imo':         inner.get('ImoNumber', 0) or 0,
                    'destination': (inner.get('Destination', '') or '').strip(),
                }
            return

        if msg_type == 'StaticDataReport':
            # AIS Type 24: Class B static data.
            # AISstream structure: PartNumber is bool (false=Part A name, true=Part B type/dims).
            # ReportB.ShipType, ReportB.Dimension.{A=bow,B=stern,C=port,D=stbd}, ReportB.CallSign
            inner = msg.get('StaticDataReport', {})
            _sample_static_msg.clear()
            _sample_static_msg.update(data)
            part_b = inner.get('PartNumber', False)  # True = Part B has type+dims
            if not part_b:
                return
            mmsi = str(meta.get('MMSI') or inner.get('UserID') or '')
            if not mmsi:
                return
            report_b        = inner.get('ReportB', {})
            type_code       = report_b.get('ShipType', 0)
            category, label = _ship_category(type_code)
            dim   = report_b.get('Dimension', {})
            bow   = dim.get('A', 0) or 0
            stern = dim.get('B', 0) or 0
            port  = dim.get('C', 0) or 0
            stbd  = dim.get('D', 0) or 0
            with _vessels_lock:
                existing = _vessel_static.get(mmsi, {})
                _vessel_static[mmsi] = {
                    'category':    category,
                    'type_label':  label,
                    'length':      bow + stern or existing.get('length', 0),
                    'beam':        port + stbd or existing.get('beam', 0),
                    'draught':     existing.get('draught', 0),
                    'callsign':    (report_b.get('CallSign', '') or '').strip() or existing.get('callsign', ''),
                    'imo':         existing.get('imo', 0),
                    'destination': existing.get('destination', ''),
                }
            return

        if msg_type not in ('PositionReport', 'StandardClassBPositionReport'):
            return

        _sample_msg.clear()
        _sample_msg.update(data)

        # AISstream nests the actual fields under Message[MessageType]
        inner = msg.get(msg_type, {})

        mmsi = meta.get('MMSI') or inner.get('UserID')
        if not mmsi:
            return

        # Position: prefer MetaData (station-corrected), fall back to inner
        lat = meta.get('latitude')  or inner.get('Latitude')
        lon = meta.get('longitude') or inner.get('Longitude')
        if lat is None or lon is None:
            return

        cog = inner.get('Cog', 0)
        if cog >= 360:
            cog = 0

        sog       = inner.get('Sog', 0)
        ship_type = inner.get('Type', 0)
        name      = meta.get('ShipName', '')
        hdg_raw   = inner.get('TrueHeading')
        heading   = None if (hdg_raw is None or hdg_raw == 511) else hdg_raw

        _update_vessel(mmsi, name, lat, lon, sog, cog, heading, ship_type)

    except Exception as e:
        log.debug(f'AIS parse error: {e}')


def _ais_loop():
    if not AIS_API_KEY:
        log.warning('AIS_API_KEY not set — vessel layer disabled')
        return

    log.info('AIS: starting WebSocket thread')
    reconnect_delay = 5

    while True:
        try:
            ws = websocket.create_connection(AIS_WS_URL, timeout=30)
            ws.send(json.dumps({
                'APIKey':       AIS_API_KEY,
                'BoundingBoxes': AIS_BBOX,
            }))
            log.info('AIS: connected and subscribed')
            reconnect_delay = 5  # reset on successful connect

            while True:
                raw = ws.recv()
                if raw:
                    _process_ais_message(raw)

        except websocket.WebSocketConnectionClosedException:
            log.warning(f'AIS: connection closed — reconnecting in {reconnect_delay}s')
        except Exception as e:
            log.warning(f'AIS: error ({e}) — reconnecting in {reconnect_delay}s')

        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)  # exponential back-off, cap 60s


# ---------------------------------------------------------------------------
# Whale strandings — iNaturalist public API (no key required)
# ---------------------------------------------------------------------------
_strandings_cache = {'data': [], 'fetched_at': None}

# West Coast bounding box covering all AIS regions
_STRAND_SW = (32.0, -125.5)
_STRAND_NE = (49.0, -116.0)


def _fetch_strandings():
    results = []
    try:
        params = {
            'taxon_id':      152871,   # Cetacea (infraorder) in iNaturalist
            'quality_grade': 'research',
            'term_id':       17,   # controlled attribute: Alive or Dead
            'term_value_id': 19,   # value: Dead
            'swlat': _STRAND_SW[0], 'swlng': _STRAND_SW[1],
            'nelat': _STRAND_NE[0], 'nelng': _STRAND_NE[1],
            'd1':        '2020-01-01',   # last ~5 years
            'per_page':  200,
            'order':     'desc',
            'order_by':  'observed_on',
        }
        r = requests.get('https://api.inaturalist.org/v1/observations',
                         params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        for obs in r.json().get('results', []):
            loc = obs.get('location')
            if not loc:
                continue
            lat_s, lon_s = loc.split(',')
            taxon = obs.get('taxon') or {}
            results.append({
                'id':          obs['id'],
                'lat':         float(lat_s),
                'lon':         float(lon_s),
                'species':     taxon.get('name', 'Unknown'),
                'common_name': taxon.get('preferred_common_name', ''),
                'date':        obs.get('observed_on', ''),
                'place':       obs.get('place_guess', ''),
                'url':         obs.get('uri', ''),
            })
        log.info(f'Strandings: {len(results)} records from iNaturalist')
    except Exception as e:
        log.warning(f'Strandings fetch error: {e}')
    return results


# Start background threads
threading.Thread(target=_ais_loop,       daemon=True, name='ais-ws').start()
threading.Thread(target=_expire_vessels, daemon=True, name='ais-expire').start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'map.html')


@app.route('/api/sightings')
def sightings():
    days = max(1, min(int(request.args.get('days', DEFAULT_DAYS)), 30))
    end_date   = date.today()
    start_date = end_date - timedelta(days=days)

    params = {
        'BBOX':      request.args.get('bbox', '-180,-90,180,90'),
        'start':     str(start_date),
        'end':       str(end_date),
        'source':    'whale_alert',
        'moderated': 1,
    }
    if request.args.get('species'):
        params['species'] = request.args['species']

    try:
        r = requests.get(SIGHTINGS_URL, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 502

    return jsonify({
        'count':   data.get('count', 0),
        'results': data.get('results', []),
        'start':   str(start_date),
        'end':     str(end_date),
    })


@app.route('/api/strandings')
def strandings():
    now = datetime.now(timezone.utc)
    if (_strandings_cache['fetched_at'] is None or
            (now - _strandings_cache['fetched_at']).total_seconds() > 3600):
        _strandings_cache['data']       = _fetch_strandings()
        _strandings_cache['fetched_at'] = now
        log.info(f'Strandings: cached {len(_strandings_cache["data"])} records')
    return jsonify({'count': len(_strandings_cache['data']),
                    'strandings': _strandings_cache['data']})


@app.route('/api/debug')
def debug():
    return jsonify(_sample_msg)


@app.route('/api/debug/static')
def debug_static():
    return jsonify(_sample_static_msg)


@app.route('/api/vessels')
def vessels():
    _static_defaults = {
        'category': 'unknown', 'type_label': 'Unknown',
        'length': 0, 'beam': 0, 'draught': 0,
        'callsign': '', 'imo': 0, 'destination': '',
    }
    with _vessels_lock:
        v_list = []
        for mmsi, v in _vessels.items():
            entry = dict(v)
            entry.update(_vessel_static.get(mmsi, _static_defaults))
            t = _tracks.get(mmsi)
            entry['track'] = list(t) if t and len(t) > 1 else []
            v_list.append(entry)
    return jsonify({
        'count':   len(v_list),
        'vessels': v_list,
        'ais_key': bool(AIS_API_KEY),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=True, use_reloader=False)
