#!/usr/bin/env python3
"""
Whale Alert sighting collector — maplify.com API edition.

Queries the Conserve.IO public sightings search API for new Whale Alert
observations and writes them to a local SQLite database.  Designed to run
on a cron (every 2 hours is typical).  Deduplication is by sighting ID so
overlapping date windows are safe.

Usage:
    python3 collector.py           # normal run (last 2 days, global)
    python3 collector.py --reset   # wipe DB and state, re-pull last 2 days
    python3 collector.py --report  # show unresolved admin-log issues
"""

import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIGHTINGS_URL = 'https://maplify.com/waseak/php/search-all-sightings.php'

DB_FILE    = Path('sightings.db')
STATE_FILE = Path('state.json')

# Days of history to pull on each run.  New sightings may appear or be
# moderated up to 48 hours after creation, so 2 keeps things current.
LOOKBACK_DAYS = 2

# Moderation filter — 1 = confirmed only, omit for all states
MODERATED = 1

REQUEST_TIMEOUT = 30   # seconds

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id              INTEGER PRIMARY KEY,   -- maplify sighting ID (globally unique)
    trip_id         INTEGER,
    project_id      INTEGER,
    name            TEXT,                  -- common name (from API)
    scientific_name TEXT,                  -- scientific name (from API)
    number_sighted  INTEGER,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    created         TEXT NOT NULL,         -- UTC datetime string from API
    photo_url       TEXT,
    comments        TEXT,
    in_ocean        INTEGER,
    count_check     INTEGER,
    moderated       INTEGER,               -- 1=confirmed, 2=unconfirmed, 3=likely false
    trusted         INTEGER,               -- 1=trusted observer
    source          TEXT,
    usernm          TEXT,
    ingested_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_created    ON sightings (created);
CREATE INDEX IF NOT EXISTS idx_species    ON sightings (scientific_name);
CREATE INDEX IF NOT EXISTS idx_location   ON sightings (latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_trip       ON sightings (trip_id);

-- Admin log for data quality issues that need human attention.
CREATE TABLE IF NOT EXISTS admin_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at  TEXT NOT NULL,
    category   TEXT NOT NULL,
    detail     TEXT NOT NULL,
    sighting_id INTEGER,
    resolved   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_log_category ON admin_log (category, resolved);
"""


def open_db():
    con = sqlite3.connect(DB_FILE)
    con.executescript(SCHEMA)
    con.commit()
    return con


def log_admin(con, category, detail, sighting_id=None):
    exists = con.execute(
        'SELECT 1 FROM admin_log WHERE category=? AND detail=? AND resolved=0',
        (category, detail)
    ).fetchone()
    if not exists:
        con.execute(
            'INSERT INTO admin_log (logged_at, category, detail, sighting_id) VALUES (?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(), category, detail, sighting_id)
        )


def insert_sighting(con, s):
    """INSERT OR IGNORE — sighting ID is the primary key, so duplicates are skipped."""
    con.execute("""
        INSERT OR IGNORE INTO sightings
            (id, trip_id, project_id, name, scientific_name, number_sighted,
             latitude, longitude, created, photo_url, comments,
             in_ocean, count_check, moderated, trusted, source, usernm, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        s['id'],
        s.get('trip_id'),
        s.get('project_id'),
        s.get('name'),
        s.get('scientific_name'),
        s.get('number_sighted'),
        s['latitude'],
        s['longitude'],
        s.get('created'),
        s.get('photo_url') or None,
        s.get('comments') or None,
        s.get('in_ocean'),
        s.get('count_check'),
        s.get('moderated'),
        s.get('trusted'),
        s.get('source'),
        s.get('usernm') or None,
        datetime.now(timezone.utc).isoformat(),
    ))
    return con.execute('SELECT changes()').fetchone()[0]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def fetch_sightings(start: date, end: date, bbox=None, moderated=None, source='whale_alert'):
    """
    Returns the list of sighting dicts from the maplify API.
    bbox: (west, south, east, north) tuple, or None for global.
    """
    params = {
        'start':  str(start),
        'end':    str(end),
        'source': source,
        'BBOX':   '{},{},{},{}'.format(*(bbox or (-180, -90, 180, 90))),
    }
    if moderated is not None:
        params['moderated'] = moderated

    r = requests.get(SIGHTINGS_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get('results', [])


# ---------------------------------------------------------------------------
# Main collect loop
# ---------------------------------------------------------------------------
def collect():
    state = load_state()
    con   = open_db()

    end_date   = date.today()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    log.info(f'Fetching whale_alert sightings {start_date} → {end_date} (global)')

    try:
        results = fetch_sightings(start_date, end_date, moderated=MODERATED)
    except requests.exceptions.RequestException as e:
        log.error(f'API request failed: {e}')
        return 0

    log.info(f'API returned {len(results)} sightings')

    new_count = 0
    for s in results:
        lat = s.get('latitude')
        lon = s.get('longitude')

        # Basic coordinate sanity — API should always provide valid coords,
        # but log if something looks wrong.
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            log_admin(con, 'bad_coordinates',
                      f'id={s.get("id")} lat={lat} lon={lon}', s.get('id'))
            continue

        if s.get('in_ocean') == 0:
            log_admin(con, 'on_land',
                      f'id={s.get("id")} {s.get("name")} @ ({lat:.4f},{lon:.4f})',
                      s.get('id'))

        changed = insert_sighting(con, s)
        if changed:
            new_count += 1
            log.info(
                f'  [{s["id"]}] {s.get("created","")[:16]}  '
                f'{s.get("number_sighted","?")}x {s.get("name")}  '
                f'@ ({lat:.4f}, {lon:.4f})'
                + (f'  trusted' if s.get('trusted') else '')
            )

    con.commit()
    con.close()

    state['last_run']      = datetime.now(timezone.utc).isoformat()
    state['last_start']    = str(start_date)
    state['last_end']      = str(end_date)
    state['last_new_count'] = new_count
    save_state(state)

    log.info(f'Done — {new_count} new sightings ingested (of {len(results)} returned)')
    return new_count


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def report():
    if not DB_FILE.exists():
        print('No database found.')
        return

    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        'SELECT id, logged_at, category, detail, sighting_id '
        'FROM admin_log WHERE resolved=0 ORDER BY logged_at DESC'
    ).fetchall()

    if not rows:
        print('No open admin issues.')
    else:
        print(f'{len(rows)} open issue(s):\n')
        for r in rows:
            print(f'  [{r["id"]}] {r["logged_at"][:16]}  {r["category"]:20s}  {r["detail"]}'
                  + (f'  (sighting {r["sighting_id"]})' if r['sighting_id'] else ''))
        print('\nTo resolve: UPDATE admin_log SET resolved=1 WHERE id=<id>;')

    # Summary stats
    total = con.execute('SELECT COUNT(*) FROM sightings').fetchone()[0]
    earliest = con.execute('SELECT MIN(created) FROM sightings').fetchone()[0]
    latest   = con.execute('SELECT MAX(created) FROM sightings').fetchone()[0]
    print(f'\nDB: {total} sightings  ({earliest[:10] if earliest else "—"} → {latest[:10] if latest else "—"})')
    con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if '--reset' in sys.argv:
        for f in (DB_FILE, STATE_FILE):
            if f.exists():
                f.unlink()
                log.info(f'Deleted {f}')

    if '--report' in sys.argv:
        report()
    else:
        collect()
