#!/usr/bin/env python3
"""
Fetch recent Whale Alert (project 7) sightings by probing sequential trip IDs
above the last-seen ID stored in state.json.

Trip IDs are globally sequential across all projects. We detect project 7 trips
by the presence of 'Whale Alert Species' in the sighting data. We stop probing
on the first 404, which means we've reached the frontier of existing trips.

State file (state.json) tracks the highest trip ID seen so far so each run
only checks new IDs. First run seeds from the known max ID in our cached dump.
"""

import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from requests.auth import HTTPBasicAuth

AUTH      = HTTPBasicAuth('pointBlueAPI', '$Whales#Rfun!')
SPT_BASE  = 'https://spotter.conserve.io/spotter'
STATE_FILE = Path('state.json')

# Highest project 7 trip ID from our initial full dump (2026-05-20).
# Used only when state.json doesn't exist yet.
BOOTSTRAP_ID = 159234

# Max IDs to probe in a single run (safety cap; normal runs stop on 404).
MAX_PROBE = 2000


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {'last_id': BOOTSTRAP_ID}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_whale_alert(detail):
    return any('Whale Alert Species' in s for s in detail.get('sightings', []))


def fetch_trip(trip_id):
    r = requests.get(f'{SPT_BASE}/trip/{trip_id}/data',
                     auth=AUTH, params={'format': 'json'}, timeout=15)
    if r.status_code == 404:
        return None   # past the frontier
    r.raise_for_status()
    return r.json()


def fetch_new_sightings(days=30):
    """
    Probe trip IDs above the last-seen ID. Yields (trip_id, sighting) tuples
    for Whale Alert sightings created within the last `days` days.
    Updates state.json with the highest ID reached.
    """
    state   = load_state()
    last_id = state['last_id']
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)

    print(f'Last seen ID: {last_id}')
    print(f'Probing from {last_id + 1} (cutoff: {cutoff.date()})\n')

    highest_seen = last_id
    checked = 0

    for trip_id in range(last_id + 1, last_id + MAX_PROBE + 1):
        detail = fetch_trip(trip_id)
        checked += 1

        if detail is None:
            # First 404 = frontier reached, nothing more exists yet
            print(f'Frontier reached at ID {trip_id} ({checked} IDs checked)')
            break

        highest_seen = trip_id

        if not is_whale_alert(detail):
            continue

        created = detail.get('create_date', '')
        try:
            dt = datetime.fromisoformat(str(created).replace(' ', 'T'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None

        if dt and dt < cutoff:
            continue  # too old

        for sighting in detail.get('sightings', []):
            yield trip_id, detail, sighting

    state['last_id'] = highest_seen
    save_state(state)
    print(f'State updated: last_id={highest_seen}')


def summarise(trip_id, detail, sighting):
    species = sighting.get('Whale Alert Species', 'Unknown')
    count   = sighting.get('Number Sighted', '?')
    lat     = sighting.get('device_latitude', '?')
    lon     = sighting.get('device_longitude', '?')
    created = str(detail.get('create_date', ''))[:16]
    submitter = sighting.get('Whale Alert Submitter Name', '')
    comments  = sighting.get('Comments', '')
    print(f'  [{trip_id}] {created}  {count}x {species}  @ ({lat}, {lon})'
          + (f'  — {submitter}' if submitter else '')
          + (f'\n         comment: {comments}' if comments else ''))


if __name__ == '__main__':
    results = list(fetch_new_sightings(days=30))
    print(f'\nNew Whale Alert sightings in last 30 days: {len(results)}\n')
    for trip_id, detail, sighting in results:
        summarise(trip_id, detail, sighting)
