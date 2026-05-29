#!/usr/bin/env python3
"""
Conserve.IO Spotter API explorer.
Prints a structured summary of the API at three tiers:
  1. All projects (id, name, form names)
  2. Trips for projects 2, 3, 7 (most recent N)
  3. Full detail dump of one trip per project (sightings + track)
"""

import json
import sys
import requests
from requests.auth import HTTPBasicAuth

AUTH     = HTTPBasicAuth('pointBlueAPI', '$Whales#Rfun!')
API_BASE = 'https://spotter.conserve.io/api/v1'
SPT_BASE = 'https://spotter.conserve.io/spotter'

TARGET_PROJECTS = [2, 3, 7]
TRIPS_TO_SHOW   = 3   # how many recent trips to list per project
TRIPS_TO_EXPAND = 1   # how many trips to fully dump per project


TIMEOUT = 120  # seconds

def get(url):
    r = requests.get(url, auth=AUTH, params={'format': 'json'}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def section(title):
    print(f'\n{"━"*70}')
    print(f'  {title}')
    print(f'{"━"*70}')


def dump(label, data):
    print(f'\n--- {label} ---')
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Tier 1: project list
# ---------------------------------------------------------------------------
section('TIER 1 — All projects')
projects = get(f'{API_BASE}/project')
print(f'Total projects: {projects["meta"]["total_count"]}\n')

project_map = {}
for p in projects['objects']:
    project_map[p['id']] = p['name']
    form_names = [f['name'] for f in p.get('forms', [])]
    print(f"  [{p['id']:>3}] {p['name']}")
    for f in p.get('forms', []):
        field_names = [ff['field']['name'] for ff in f.get('form_fields', [])]
        print(f"         form: {f['name']}  →  fields: {', '.join(field_names)}")

# ---------------------------------------------------------------------------
# Tier 2: trips per target project
# ---------------------------------------------------------------------------
for project_id in TARGET_PROJECTS:
    section(f'TIER 2 — Project {project_id}: {project_map.get(project_id, "?")} — recent trips')

    try:
        data = get(f'{SPT_BASE}/project/{project_id}/trip_data')
    except requests.exceptions.Timeout:
        print(f'  TIMEOUT fetching trip list (>{TIMEOUT}s) — skipping')
        continue
    except Exception as e:
        print(f'  ERROR: {e}')
        continue

    trips = data.get('trips', [])
    print(f'Total trips returned: {len(trips)}\n')

    for trip in trips[:TRIPS_TO_SHOW]:
        print(f"  trip {trip['id']:>6}  start={trip.get('start_date','')}  end={trip.get('end_date','')}  created={trip.get('create_date','')}")

    # ---------------------------------------------------------------------------
    # Tier 3: full detail for the most recent trip
    # ---------------------------------------------------------------------------
    for trip in trips[:TRIPS_TO_EXPAND]:
        section(f'TIER 3 — Trip {trip["id"]} detail (project {project_id})')
        try:
            detail = get(f'{SPT_BASE}/trip/{trip["id"]}/data')
        except requests.exceptions.Timeout:
            print(f'  TIMEOUT fetching trip detail (>{TIMEOUT}s) — skipping')
            continue
        except Exception as e:
            print(f'  ERROR: {e}')
            continue

        # Top-level keys
        print(f'Top-level keys: {list(detail.keys())}\n')

        # Track summary
        track = detail.get('track')
        if track:
            pts = track.get('gpx', {}).get('trk', {}).get('trkseg', {}).get('trkpt', [])
            print(f'Track: {len(pts)} GPS point(s)')
            if pts:
                print(f'  first point: {pts[0]}')
                print(f'  last  point: {pts[-1]}')
        else:
            print('Track: none')

        # Sightings
        sightings = detail.get('sightings', [])
        print(f'\nSightings: {len(sightings)}')
        if sightings:
            print('  Keys in first sighting:')
            for k, v in sightings[0].items():
                print(f'    {k}: {v}')

        # Everything else (weather, effort, etc.)
        other_keys = [k for k in detail.keys() if k not in ('track', 'sightings')]
        for k in other_keys:
            val = detail[k]
            if isinstance(val, list):
                print(f'\n{k}: {len(val)} item(s)')
                if val:
                    print(f'  Keys in first item: {list(val[0].keys()) if isinstance(val[0], dict) else val[0]}')
                    print(json.dumps(val[0], indent=4))
            else:
                print(f'\n{k}: {json.dumps(val, indent=2)}')
