#!/usr/bin/env python3
"""
Fetch Whale Alert (project 7) trips from the last N days using streaming JSON
parsing so we never download the full 87k-trip response.

The API returns trips newest-first, so we stop as soon as we hit a trip
older than the cutoff — typically in the first few KB of the response.
"""

import ijson
import requests
from datetime import datetime, timezone, timedelta
from requests.auth import HTTPBasicAuth

AUTH     = HTTPBasicAuth('pointBlueAPI', '$Whales#Rfun!')
SPT_BASE = 'https://spotter.conserve.io/spotter'


def parse_date(s):
    if not s:
        return None
    # API returns "2026-04-20 14:32:00+00:00"
    return datetime.fromisoformat(str(s).replace(' ', 'T'))


def fetch_recent_trips(project_id=7, days=30):
    """
    Stream the trip list for project_id, yielding only trips created
    within the last `days` days. Stops parsing as soon as the stream
    passes the cutoff date.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    url    = f'{SPT_BASE}/project/{project_id}/trip_data'

    print(f'Streaming trip list for project {project_id} (cutoff: {cutoff.date()})...')

    # Server buffers the full response before sending — needs a long read timeout.
    # (connect_timeout=10s, read_timeout=600s)
    with requests.get(url, auth=AUTH, params={'format': 'json'},
                      stream=True, timeout=(10, 600)) as resp:
        resp.raise_for_status()

        count_yielded = 0
        count_skipped = 0

        for trip in ijson.items(resp.raw, 'trips.item'):
            created = parse_date(trip.get('create_date'))

            if created is None:
                continue

            if created < cutoff:
                # Trips are newest-first; once we're past the cutoff we're done
                print(f'  → reached cutoff at trip {trip["id"]} ({created.date()}), stopping.')
                break

            count_yielded += 1
            yield trip

        print(f'  → {count_yielded} recent trips found.')


def fetch_trip_detail(trip_id):
    url = f'{SPT_BASE}/trip/{trip_id}/data'
    r   = requests.get(url, auth=AUTH, params={'format': 'json'}, timeout=30)
    r.raise_for_status()
    return r.json()


if __name__ == '__main__':
    trips = list(fetch_recent_trips(project_id=7, days=30))

    print(f'\nTrips in last 30 days: {len(trips)}')
    if trips:
        print('\nMost recent 5:')
        for t in trips[:5]:
            print(f"  [{t['id']:>6}] created={str(t.get('create_date',''))[:10]}  creator={t.get('creator','')}")

        print('\nFetching detail for most recent trip...')
        detail = fetch_trip_detail(trips[0]['id'])
        sightings = detail.get('sightings', [])
        print(f'  Sightings: {len(sightings)}')
        for s in sightings:
            species = s.get('Whale Alert Species', '?')
            count   = s.get('Number Sighted', '?')
            lat     = s.get('device_latitude', '?')
            lon     = s.get('device_longitude', '?')
            print(f'    {count}x {species} @ ({lat}, {lon})')
