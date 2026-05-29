#!/usr/bin/env python3
"""
Whale Alert SMS alerter — AWS SNS edition.

For each active subscription, fetches recent Whale Alert sightings within the
configured bounding box and sends an SMS via AWS SNS for every new sighting
not yet recorded in alert_log.

Intended to run on a cron (every 2 hours is typical).

Usage:
    python3 alerter.py              # normal run
    python3 alerter.py --dry-run    # log what would be sent, no SMS
    python3 alerter.py --catch-up   # mark current window as alerted, no SMS
                                    # (use when adding a new subscription to
                                    #  avoid a burst of back-fill messages)

AWS credentials:
    boto3 resolves credentials in the standard order:
    1. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION env vars
    2. ~/.aws/credentials + ~/.aws/config
    3. IAM instance role (if running on EC2)
    The SNS SMS service is available in most regions; us-east-1 is recommended.
"""

import argparse
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIGHTINGS_URL   = 'https://maplify.com/waseak/php/search-all-sightings.php'
DB_FILE         = Path('sightings.db')
LOOKBACK_DAYS   = 2      # query window per run; alert_log handles deduplication
REQUEST_TIMEOUT = 30     # seconds
SMS_MAX_LEN     = 160    # GSM-7 single-segment limit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (merged into sightings.db alongside the collector's tables)
# ---------------------------------------------------------------------------
ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    phone             TEXT NOT NULL,
    bbox_west         REAL NOT NULL,
    bbox_south        REAL NOT NULL,
    bbox_east         REAL NOT NULL,
    bbox_north        REAL NOT NULL,
    species           TEXT,
    trusted_only      INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1,
    paid_through      TEXT,        -- YYYY-MM-DD; NULL = no expiry (comp/internal)
    stripe_payment_id TEXT,        -- Stripe payment/customer ID for reference
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    sighting_id     INTEGER NOT NULL,
    alerted_at      TEXT NOT NULL,
    UNIQUE (subscription_id, sighting_id)
);

CREATE INDEX IF NOT EXISTS idx_alert_log
    ON alert_log (subscription_id, sighting_id);
"""

# Columns added after initial release — ALTER TABLE is idempotent via the helper.
MIGRATIONS = [
    ('subscriptions', 'paid_through',      'TEXT'),
    ('subscriptions', 'stripe_payment_id', 'TEXT'),
]


def _migrate(con):
    for table, column, typedef in MIGRATIONS:
        try:
            con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {typedef}')
        except sqlite3.OperationalError:
            pass  # column already exists


def open_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript(ALERT_SCHEMA)
    _migrate(con)
    con.commit()
    return con


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def fetch_sightings(sub):
    end_date   = date.today()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    params = {
        'BBOX':      f'{sub["bbox_west"]},{sub["bbox_south"]},{sub["bbox_east"]},{sub["bbox_north"]}',
        'start':     str(start_date),
        'end':       str(end_date),
        'source':    'whale_alert',
        'moderated': 1,
    }
    if sub['species']:
        params['species'] = sub['species']
    if sub['trusted_only']:
        params['trusted'] = 1

    r = requests.get(SIGHTINGS_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get('results', [])


# ---------------------------------------------------------------------------
# SMS formatting
# ---------------------------------------------------------------------------
def _strip_html(text):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text)).strip()


def format_sms(s, sub_name):
    """Build a ≤160-char SMS message for a single sighting."""
    name   = s.get('name') or s.get('scientific_name') or 'Unknown species'
    count  = s.get('number_sighted') or '?'
    lat    = float(s.get('latitude',  0))
    lon    = float(s.get('longitude', 0))
    ts     = str(s.get('created', ''))[:16]

    lat_str = f'{abs(lat):.4f}{"N" if lat >= 0 else "S"}'
    lon_str = f'{abs(lon):.4f}{"W" if lon <  0 else "E"}'

    lines = [
        f'Whale Alert: {count}x {name}',
        f'{lat_str} {lon_str}',
        f'{ts} UTC',
    ]
    if sub_name:
        lines.append(f'[{sub_name}]')

    msg = '\n'.join(lines)

    # Append comments if they fit
    raw_comments = s.get('comments') or ''
    if raw_comments:
        clean = _strip_html(raw_comments)
        remaining = SMS_MAX_LEN - len(msg) - 1
        if remaining >= 20:
            snippet = clean if len(clean) <= remaining else clean[:remaining - 3] + '...'
            msg += '\n' + snippet

    return msg[:SMS_MAX_LEN]


# ---------------------------------------------------------------------------
# SNS
# ---------------------------------------------------------------------------
def send_sms(phone, message):
    sns = boto3.client('sns')
    response = sns.publish(
        PhoneNumber=phone,
        Message=message,
        MessageAttributes={
            'AWS.SNS.SMS.SMSType': {
                'DataType':    'String',
                'StringValue': 'Transactional',
            }
        },
    )
    return response['MessageId']


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------
def run(dry_run=False, catch_up=False):
    if dry_run:
        log.info('DRY RUN — no SMS will be sent')
    if catch_up:
        log.info('CATCH-UP — marking sightings as alerted without sending SMS')

    con = open_db()
    subs = con.execute('SELECT * FROM subscriptions WHERE active=1').fetchall()

    if not subs:
        log.info('No active subscriptions.')
        con.close()
        return

    log.info(f'{len(subs)} active subscription(s)')
    today      = date.today()
    warn_date  = today + timedelta(days=7)   # warn if expiring within a week
    total_sent = 0

    for sub in subs:
        sub_id   = sub['id']
        sub_name = sub['name']
        phone    = sub['phone']

        # Payment expiry check
        paid_through = sub['paid_through']
        if paid_through:
            pt = date.fromisoformat(paid_through)
            if pt < today:
                log.warning(f'[{sub_id}] {sub_name} — EXPIRED {paid_through}, skipping')
                continue
            if pt <= warn_date:
                log.warning(f'[{sub_id}] {sub_name} — expires {paid_through} (renew soon)')

        log.info(f'[{sub_id}] {sub_name} ({phone})')

        try:
            results = fetch_sightings(sub)
        except requests.exceptions.RequestException as e:
            log.error(f'  API error: {e}')
            continue

        new_count = 0
        for s in results:
            sid = s['id']

            # Skip already-alerted sightings (UNIQUE constraint in alert_log)
            exists = con.execute(
                'SELECT 1 FROM alert_log WHERE subscription_id=? AND sighting_id=?',
                (sub_id, sid)
            ).fetchone()
            if exists:
                continue

            message = format_sms(s, sub_name)

            if not dry_run and not catch_up:
                try:
                    msg_id = send_sms(phone, message)
                    log.info(
                        f'  Sent [{sid}] {s.get("number_sighted")}x {s.get("name")}'
                        f' @ ({s.get("latitude"):.4f}, {s.get("longitude"):.4f})'
                        f'  MessageId={msg_id}'
                    )
                except (BotoCoreError, ClientError) as e:
                    log.error(f'  SNS error for sighting {sid}: {e}')
                    continue
            else:
                action = 'CATCH-UP' if catch_up else 'DRY RUN'
                log.info(
                    f'  [{action}] [{sid}] {s.get("number_sighted")}x {s.get("name")}'
                    f' @ ({s.get("latitude"):.4f}, {s.get("longitude"):.4f})\n'
                    f'  Message: {message!r}'
                )

            # Record in alert_log regardless of dry_run so re-runs are idempotent
            # (skip only in true dry-run — catch-up always records)
            if not dry_run:
                con.execute(
                    'INSERT OR IGNORE INTO alert_log '
                    '(subscription_id, sighting_id, alerted_at) VALUES (?,?,?)',
                    (sub_id, sid, datetime.now(timezone.utc).isoformat()),
                )
                new_count += 1
                total_sent += 1

        con.commit()
        log.info(f'  {new_count} new alert(s) for subscription [{sub_id}]')

    con.close()
    log.info(f'Done — {total_sent} total alert(s) sent')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Send whale sighting SMS alerts.')
    parser.add_argument('--dry-run',  action='store_true',
                        help='Log messages without sending or recording')
    parser.add_argument('--catch-up', action='store_true',
                        help='Mark current window as alerted without sending SMS')
    args = parser.parse_args()

    if args.dry_run and args.catch_up:
        parser.error('--dry-run and --catch-up are mutually exclusive')

    run(dry_run=args.dry_run, catch_up=args.catch_up)
