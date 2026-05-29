#!/usr/bin/env python3
"""
Manage whale alert subscriptions.

Usage:
    python3 subs.py list
    python3 subs.py add --name "SF Bay" --phone "+14155551234" \\
                        --west -122.6 --south 37.4 --east -121.9 --north 38.1 \\
                        --paid-through 2026-06-20 --stripe-id pi_abc123
    python3 subs.py add --name "Puget Sound Orcas" --phone "+12065551234" \\
                        --west -125 --south 47 --east -122 --north 49.5 \\
                        --species "Orcinus orca" --trusted \\
                        --paid-through 2026-06-20
    python3 subs.py renew <id> --months 1 [--stripe-id pi_abc123]
    python3 subs.py renew <id> --until 2026-12-31
    python3 subs.py disable <id>
    python3 subs.py enable  <id>
    python3 subs.py delete  <id>
    python3 subs.py history <id>

Phone numbers must be in E.164 format: +1XXXXXXXXXX (US), +44XXXXXXXXXX (UK), etc.
--paid-through: YYYY-MM-DD. Omit for internal/comp accounts (no expiry).
--stripe-id: Stripe payment or customer ID, for your records.

BBOX: four separate args --west --south --east --north (decimal degrees, negative = W or S)
See API_EXAMPLES.md for preset bounding boxes.
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_FILE = Path('sightings.db')

# Same schema/migration blocks as alerter.py — CREATE IF NOT EXISTS is idempotent
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
    paid_through      TEXT,
    stripe_payment_id TEXT,
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

MIGRATIONS = [
    ('subscriptions', 'paid_through',      'TEXT'),
    ('subscriptions', 'stripe_payment_id', 'TEXT'),
]


def open_db():
    if not DB_FILE.exists():
        sys.exit(f'Error: {DB_FILE} not found. Run collector.py first.')
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript(ALERT_SCHEMA)
    for table, column, typedef in MIGRATIONS:
        try:
            con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {typedef}')
        except sqlite3.OperationalError:
            pass
    con.commit()
    return con


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _expiry_tag(paid_through):
    """Return a display tag for the paid_through date."""
    if not paid_through:
        return 'no expiry'
    today = date.today()
    pt    = date.fromisoformat(paid_through)
    if pt < today:
        return f'EXPIRED {paid_through}'
    if pt <= today + timedelta(days=7):
        return f'expiring {paid_through} !'
    return paid_through


def cmd_list(args):
    con = open_db()
    rows = con.execute('SELECT * FROM subscriptions ORDER BY id').fetchall()
    con.close()

    if not rows:
        print('No subscriptions.')
        return

    print(f'{"ID":<4} {"St":<3} {"Name":<22} {"Phone":<16} {"Paid through":<22} {"Species":<25} Trusted')
    print('-' * 100)
    for r in rows:
        status  = 'ON ' if r['active'] else 'off'
        expiry  = _expiry_tag(r['paid_through'])
        species = r['species'] or '(all)'
        trusted = 'yes' if r['trusted_only'] else ''
        bbox    = f'  BBOX: {r["bbox_west"]},{r["bbox_south"]},{r["bbox_east"]},{r["bbox_north"]}'
        print(f'{r["id"]:<4} {status:<3} {r["name"]:<22} {r["phone"]:<16} {expiry:<22} {species:<25} {trusted}')
        print(f'     {bbox}' + (f'  stripe: {r["stripe_payment_id"]}' if r['stripe_payment_id'] else ''))


def _parse_date(val, argname):
    try:
        return str(date.fromisoformat(val))
    except ValueError:
        sys.exit(f'Error: {argname} must be YYYY-MM-DD, got: {val}')


def cmd_add(args):
    west, south, east, north = args.west, args.south, args.east, args.north

    if south >= north:
        sys.exit(f'Error: --south ({south}) must be less than --north ({north})')
    if not args.phone.startswith('+'):
        sys.exit('Error: --phone must be in E.164 format, e.g. +14155551234')

    paid_through = _parse_date(args.paid_through, '--paid-through') if args.paid_through else None

    con = open_db()
    cur = con.execute("""
        INSERT INTO subscriptions
            (name, phone, bbox_west, bbox_south, bbox_east, bbox_north,
             species, trusted_only, active, paid_through, stripe_payment_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
    """, (
        args.name,
        args.phone,
        west, south, east, north,
        args.species or None,
        1 if args.trusted else 0,
        paid_through,
        args.stripe_id or None,
        datetime.now(timezone.utc).isoformat(),
    ))
    con.commit()
    new_id = cur.lastrowid
    con.close()

    print(f'Added subscription [{new_id}]: {args.name}')
    print(f'  Phone:       {args.phone}')
    print(f'  BBOX:        W={west} S={south} E={east} N={north}')
    print(f'  Species:     {args.species or "(all)"}')
    print(f'  Trusted:     {"yes" if args.trusted else "no"}')
    print(f'  Paid through: {paid_through or "(no expiry)"}')
    if args.stripe_id:
        print(f'  Stripe ID:   {args.stripe_id}')
    print()
    print('To avoid a back-fill SMS burst on first run:')
    print('  python3 alerter.py --catch-up')


def cmd_renew(args):
    con = open_db()
    sub = con.execute('SELECT * FROM subscriptions WHERE id=?', (args.id,)).fetchone()
    if not sub:
        con.close()
        sys.exit(f'No subscription with id {args.id}.')

    # Calculate new paid_through date
    if args.until:
        new_date = _parse_date(args.until, '--until')
    else:
        # Extend from whichever is later: today or current paid_through
        today = date.today()
        current = date.fromisoformat(sub['paid_through']) if sub['paid_through'] else today
        base = max(today, current)
        # Add calendar months
        month = base.month - 1 + args.months
        new_date = str(base.replace(year=base.year + month // 12, month=month % 12 + 1))

    updates = {'paid_through': new_date}
    if args.stripe_id:
        updates['stripe_payment_id'] = args.stripe_id

    set_clause = ', '.join(f'{k}=?' for k in updates)
    con.execute(
        f'UPDATE subscriptions SET {set_clause} WHERE id=?',
        (*updates.values(), args.id)
    )
    con.commit()
    con.close()

    print(f'Renewed subscription [{args.id}]: {sub["name"]}')
    print(f'  Paid through: {new_date}')
    if args.stripe_id:
        print(f'  Stripe ID:    {args.stripe_id}')


def _set_active(sub_id, active, verb):
    con = open_db()
    changed = con.execute(
        'UPDATE subscriptions SET active=? WHERE id=?', (active, sub_id)
    ).rowcount
    con.commit()
    con.close()
    if changed:
        print(f'Subscription [{sub_id}] {verb}.')
    else:
        print(f'No subscription with id {sub_id}.')


def cmd_disable(args): _set_active(args.id, 0, 'disabled')
def cmd_enable(args):  _set_active(args.id, 1, 'enabled')


def cmd_delete(args):
    con = open_db()
    sub = con.execute('SELECT name FROM subscriptions WHERE id=?', (args.id,)).fetchone()
    if not sub:
        con.close()
        print(f'No subscription with id {args.id}.')
        return

    confirm = input(f'Delete [{args.id}] "{sub["name"]}" and its alert history? [y/N] ')
    if confirm.strip().lower() != 'y':
        print('Aborted.')
        con.close()
        return

    con.execute('DELETE FROM alert_log    WHERE subscription_id=?', (args.id,))
    con.execute('DELETE FROM subscriptions WHERE id=?',             (args.id,))
    con.commit()
    con.close()
    print(f'Deleted subscription [{args.id}].')


def cmd_history(args):
    con = open_db()
    sub = con.execute('SELECT * FROM subscriptions WHERE id=?', (args.id,)).fetchone()
    if not sub:
        con.close()
        print(f'No subscription with id {args.id}.')
        return

    print(f'Subscription [{args.id}]: {sub["name"]}')

    rows = con.execute("""
        SELECT al.sighting_id, al.alerted_at,
               s.name, s.number_sighted, s.latitude, s.longitude, s.created
        FROM alert_log al
        LEFT JOIN sightings s ON s.id = al.sighting_id
        WHERE al.subscription_id = ?
        ORDER BY al.alerted_at DESC
        LIMIT 50
    """, (args.id,)).fetchall()
    con.close()

    if not rows:
        print('No alerts sent yet.')
        return

    print(f'\n{"Sighting ID":<12} {"Alerted at":<22} {"Species":<28} {"Ct":>3} {"Created":<18}')
    print('-' * 92)
    for r in rows:
        print(
            f'{r["sighting_id"]:<12} '
            f'{str(r["alerted_at"])[:19]:<22} '
            f'{str(r["name"] or "?"):<28} '
            f'{str(r["number_sighted"] or "?"):>3} '
            f'{str(r["created"] or "")[:16]}'
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        description='Manage whale alert SMS subscriptions.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('list', help='List all subscriptions')

    p_add = sub.add_parser('add', help='Add a new subscription')
    p_add.add_argument('--name',         required=True,  help='Human-readable label')
    p_add.add_argument('--phone',        required=True,  help='Phone in E.164 format')
    p_add.add_argument('--west',         required=True,  type=float, help='BBOX west longitude')
    p_add.add_argument('--south',        required=True,  type=float, help='BBOX south latitude')
    p_add.add_argument('--east',         required=True,  type=float, help='BBOX east longitude')
    p_add.add_argument('--north',        required=True,  type=float, help='BBOX north latitude')
    p_add.add_argument('--species',      default=None,   help='Scientific name filter (optional)')
    p_add.add_argument('--trusted',      action='store_true', help='Trusted observers only')
    p_add.add_argument('--paid-through', default=None,   dest='paid_through',
                       help='Expiry date YYYY-MM-DD (omit for no expiry)')
    p_add.add_argument('--stripe-id',    default=None,   dest='stripe_id',
                       help='Stripe payment/customer ID for reference')

    p_renew = sub.add_parser('renew', help='Extend paid_through for a subscription')
    p_renew.add_argument('id', type=int, metavar='ID')
    grp = p_renew.add_mutually_exclusive_group(required=True)
    grp.add_argument('--months', type=int, help='Extend by N calendar months')
    grp.add_argument('--until',  help='Set expiry to YYYY-MM-DD')
    p_renew.add_argument('--stripe-id', default=None, dest='stripe_id',
                         help='Update Stripe payment ID')

    for cmd in ('disable', 'enable', 'delete', 'history'):
        p = sub.add_parser(cmd, help=f'{cmd.capitalize()} a subscription by ID')
        p.add_argument('id', type=int, metavar='ID')

    return parser


if __name__ == '__main__':
    parser = build_parser()
    args   = parser.parse_args()
    {
        'list':    cmd_list,
        'add':     cmd_add,
        'renew':   cmd_renew,
        'disable': cmd_disable,
        'enable':  cmd_enable,
        'delete':  cmd_delete,
        'history': cmd_history,
    }[args.command](args)
