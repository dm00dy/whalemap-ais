# Whale Map + SMS Alert System

A pitch/prototype for a paid SMS whale sighting alert service on the US West Coast.

**What it does:**
- Shows a live map of whale sightings (from the public Whale Alert network), live AIS vessel traffic, and cetacean strandings/carcasses
- Lets subscribers configure a geographic bounding box and receive SMS alerts when a new moderated sighting appears inside it
- Supports subscription expiry and Stripe payment ID tracking for a freemium-to-paid onboarding flow

This is a functional MVP — no mock data, no stubs. The map and alerter work today against real APIs.

---

## Files

| File | Purpose |
|---|---|
| `map_server.py` | Flask server — serves the map, proxies the sightings API, streams live AIS vessel positions |
| `map.html` | MapLibre GL JS map (loaded by the Flask server at `/`) |
| `collector.py` | Cron script — pulls whale sightings from maplify.com into `sightings.db` |
| `alerter.py` | Cron script — checks `sightings.db` for new sightings and sends SMS via AWS SNS |
| `subs.py` | CLI — manage subscriptions (add, renew, disable, delete, history) |
| `explore.py` | Dev utility — explore raw maplify API responses |
| `fetch_by_id.py` | Dev utility — fetch a specific sighting by ID |
| `fetch_recent.py` | Dev utility — fetch recent sightings for inspection |
| `API_EXAMPLES.md` | AISstream.io API reference and bounding box presets |

**Not in git (gitignored):**

| File | Purpose |
|---|---|
| `.env` | `AIS_API_KEY=...` — get a key at [aisstream.io](https://aisstream.io/) |
| `sightings.db` | SQLite database — sightings + subscriptions + alert log |
| `state.json` | Last-run metadata for `collector.py` |

---

## Quick Start

### 1. Install dependencies

```bash
pip install flask requests websocket-client boto3 python-dotenv
```

### 2. Configure

```bash
cp .env.example .env   # or create manually:
echo "AIS_API_KEY=your_key_here" > .env
```

AWS credentials for SNS (alerter only) — standard boto3 resolution order:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1   # recommended for SNS SMS
```

Or use `~/.aws/credentials`. IAM instance role works on EC2.

### 3. Run the map

```bash
python3 map_server.py              # http://localhost:5000
python3 map_server.py --port 8080  # custom port
```

The AIS vessel layer starts populating within 15–30 seconds. Vessel *type* (category color) arrives with the first static data broadcast from each ship — Class A every ~6 min, Class B less predictably — so arrows may show grey (unknown) for a minute or two after startup.

---

## Data Sources

### Whale sightings — maplify.com / Whale Alert
- Public API, no key required
- Same data powering the [Whale Alert mobile app](https://whalealert.org/)
- Moderated sightings only (`moderated=1`), all species by default
- `map_server.py` proxies `/api/sightings` to avoid CORS issues; `collector.py` writes to SQLite for the alerter

### Vessel traffic — AISstream.io
- Requires a free API key: [aisstream.io](https://aisstream.io/)
- WebSocket stream; `map_server.py` maintains a persistent connection in a background thread
- 11 bounding boxes covering SF Bay, Monterey Bay, Puget Sound, Strait of Juan de Fuca, Santa Barbara Channel, Ventura, Morro Bay, Channel Islands, Long Beach, San Diego, Columbia River mouth
- AIS terrestrial receiver coverage is sparse in some areas (Santa Barbara, Morro Bay, Channel Islands may show few or no vessels)
- Vessel tracks are kept in memory for up to ~1–3 hours; vessels not heard from for 30 min are expired

### Cetacean strandings — iNaturalist
- Public API, no key required
- Research-grade "Dead" cetacean observations (`taxon_id=152871`, `term_id=17&term_value_id=19`)
- Cached for 1 hour server-side; ~200 records from the US West Coast since 2020

---

## SMS Alert System

### Architecture

```
maplify.com API
       │
   collector.py  (cron every 2h)
       │
   sightings.db  (SQLite)
       │
   alerter.py    (cron every 2h)
       │
   AWS SNS  ──▶  subscriber's phone
```

### Managing subscriptions

```bash
# List all subscriptions
python3 subs.py list

# Add a subscriber
python3 subs.py add \
  --name "SF Bay Watcher" \
  --phone "+14155551234" \
  --west -122.6 --south 37.4 --east -121.9 --north 38.1 \
  --paid-through 2026-12-31 \
  --stripe-id pi_abc123

# Add with species filter and trusted-observers-only
python3 subs.py add \
  --name "Puget Sound Orcas" \
  --phone "+12065551234" \
  --west -125 --south 47 --east -122 --north 49.5 \
  --species "Orcinus orca" --trusted \
  --paid-through 2026-12-31

# Renew for 1 month (extends from current paid_through or today, whichever is later)
python3 subs.py renew 1 --months 1 --stripe-id pi_xyz456

# Renew to a specific date
python3 subs.py renew 1 --until 2027-06-01

# Disable/enable/delete
python3 subs.py disable 1
python3 subs.py enable  1
python3 subs.py delete  1

# View alert history for a subscriber
python3 subs.py history 1
```

Phone numbers must be in E.164 format: `+14155551234` (US), `+447700900123` (UK), etc.

**`--paid-through`**: omit for internal/comp accounts (no expiry). When expired, `alerter.py` logs a warning and skips the subscription — no alerts sent.

**First run after adding a subscriber**: use `--catch-up` to mark the current 2-day window as already alerted, so the subscriber doesn't get a burst of historical messages:

```bash
python3 alerter.py --catch-up
```

### Running the alerter

```bash
python3 alerter.py           # normal run
python3 alerter.py --dry-run # log what would be sent, no SMS, no DB writes
```

### Recommended cron (run as the app user)

```cron
0 */2 * * *  cd /path/to/app && python3 collector.py >> logs/collector.log 2>&1
5 */2 * * *  cd /path/to/app && python3 alerter.py  >> logs/alerter.log  2>&1
```

Stagger by 5 minutes so the collector finishes before the alerter runs.

---

## AWS SNS Setup

1. Create an AWS account (or use an existing one)
2. Ensure the IAM user/role has `sns:Publish` permission (or use `AmazonSNSFullAccess` for simplicity)
3. Request a spending limit increase in the SNS console — the default sandbox limit ($1/month) is too low for production; request at least $10–50/month to start
4. The SNS region should be `us-east-1` — it has the broadest SMS carrier support
5. SMS type is set to `Transactional` (higher deliverability, higher cost than Promotional)

AWS SNS charges approximately $0.00645/SMS to US numbers. At 10 sightings/day with 10 subscribers, that's ~$0.65/day / ~$20/month.

---

## Pricing Context (for the pitch deck)

No direct commercial competitor exists for this exact service. Closest analogues:

| Service | Audience | Price |
|---|---|---|
| Whale Alert app | General public | Free (same data source) |
| BirdGuides (UK) | Birdwatchers | £65–99/year for SMS/push alerts |
| WRAS (Ocean Wise) | Commercial vessels only | Not disclosed |

Suggested tiers:
- **Enthusiast** — $10/month or $90/year — 1 bounding box, all species, SMS
- **Professional** (whale watch operators, researchers) — $25/month — multiple boxes, priority support

Payment: create a Stripe Payment Link in the Stripe dashboard (no code needed). Activate manually via `subs.py add` after payment confirmation. Record the Stripe payment ID with `--stripe-id`.

---

## Map Features

- **Whale sightings** — circles sized by count, colored by species group; click for species, count, date, photo
- **Vessel traffic** — animated arrows colored by vessel category (cargo, tanker, passenger, fishing, sailing, etc.); click for MMSI, name, speed, dimensions, destination
- **Vessel tracks** — trail lines showing recent path (up to ~3 hours of history)
- **Strandings** — dark circles with red outline marking cetacean carcass observations; click for species, date, location, iNaturalist link
- **Species filter panel** — toggle sighting layers by species group, toggle strandings, toggle vessel tracks
- **Feed sidebar** — scrollable list of recent sightings with stranding summary count
- **Date range** — slider to view 1–14 days of sightings history

### Debug endpoints

```
GET /api/sightings    # raw sightings proxy
GET /api/vessels      # current vessel state + static data
GET /api/strandings   # cached stranding records
GET /api/debug        # last raw AIS position message (for dev)
GET /api/debug/static # last raw AIS static data message (for dev)
```

---

## Known Limitations / Future Work

- **AIS coverage gaps**: terrestrial AIS receiver networks are sparse in some areas (Morro Bay, Channel Islands, parts of Santa Barbara). A satellite AIS feed (e.g. from exactEarth or Spire) would fill these but costs money.
- **Map server is single-process**: Flask's dev server is not production-grade. For real deployment, use gunicorn with a single worker (the AIS WebSocket thread is in-process and not shared-memory-safe across workers).
- **No web-based subscription signup**: onboarding is currently manual via `subs.py`. A Stripe webhook + automated `subs.py` call would fully automate it.
- **SQLite**: fine for a few hundred subscribers. Migrate to Postgres if you expect thousands of concurrent alert queries.
- **SMS opt-out handling**: AWS SNS handles STOP/HELP/UNSTOP automatically for US numbers, but you should reflect that state in `subscriptions.active` to avoid re-subscribing users who opted out. Consider a SNS delivery status log (CloudWatch or S3).

---

## Conservation Note

[WRAS (Ocean Wise)](https://oceanwise.ca/program/wras/) restricts vessel alerts to commercial operators and explicitly excludes whale watch tours, citing concern about increased recreational traffic around whales. Consider your customer targeting and messaging carefully — whale watch operators increase vessel density near whales, while individual naturalists and photographers generally have lower impact.
