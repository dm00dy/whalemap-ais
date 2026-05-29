# Whale Alert Sightings API — Examples

Base URL: `https://maplify.com/waseak/php/search-all-sightings.php`

All responses are JSON with a `count` integer and `results` array.  
Dates are UTC. Coordinates are decimal degrees (WGS84).  
`BBOX` order: **west, south, east, north**.

## Authentication

**None required.** The API is fully open — plain GET requests with no API key, token, or credentials of any kind. The only restricted feature is the `&c` parameter (submitter contact details: name, email, phone), which is gated by server IP whitelist and intended for authorized research use only.

> Note: the older `spotter.conserve.io` API (used internally by the Conserve.IO Spotter app) requires HTTP Basic Auth. That API is not needed here — the maplify endpoint provides everything required for sightings queries without credentials.

---

## Key Parameters

| Parameter | Notes |
|---|---|
| `BBOX` | Required. `west,south,east,north` |
| `start` / `end` | `YYYY-MM-DD`. Default: prior 2 days if omitted. |
| `source` | Use `whale_alert` to restrict to citizen Whale Alert sightings. |
| `species` | Hard match on scientific name (e.g. `Orcinus orca`). |
| `moderated` | `1` = confirmed only · `2` = unconfirmed · omit = all non-false |
| `trusted` | `1` = trusted observers only |

---

## 1. Gray Whales in San Francisco Bay — last 72 hours

**BBOX:** `-122.6, 37.4, -121.9, 38.1`  
(Covers the Bay from the Golden Gate to the Carquinez Strait)

```
https://maplify.com/waseak/php/search-all-sightings.php
  ?BBOX=-122.6,37.4,-121.9,38.1
  &start=2026-05-17
  &end=2026-05-20
  &species=Eschrichtius+robustus
  &source=whale_alert
```

**One-liner:**
```
https://maplify.com/waseak/php/search-all-sightings.php?BBOX=-122.6,37.4,-121.9,38.1&start=2026-05-17&end=2026-05-20&species=Eschrichtius+robustus&source=whale_alert
```

**Notes:**
- `species=Eschrichtius+robustus` is a hard match on the scientific name — filters to Gray Whale only regardless of how the common name was entered (`Gray`, `Gray Whale`, etc.).
- Adjust `start` to `today - 3 days` on each run to cover 72 hours.
- SF Bay grays are typically seen February–May during the northward migration.

**Sample response (2026-05-20):**
```json
{
  "count": 6,
  "results": [
    {
      "id": 247071,
      "name": "Gray Whale",
      "scientific_name": "Eschrichtius robustus",
      "number_sighted": 1,
      "latitude": 37.8282,
      "longitude": -122.4686,
      "created": "2026-05-20 16:48:00",
      "moderated": 1,
      "trusted": 1,
      "source": "whale_alert"
    }
  ]
}
```

---

## 2. Orcas in Puget Sound and the Salish Sea

**BBOX:** `-125.0, 47.0, -122.0, 49.5`  
(Covers Puget Sound, the Strait of Juan de Fuca, and the southern Salish Sea)

```
https://maplify.com/waseak/php/search-all-sightings.php
  ?BBOX=-125.0,47.0,-122.0,49.5
  &start=2026-05-13
  &end=2026-05-20
  &species=Orcinus+orca
  &source=whale_alert
```

**One-liner:**
```
https://maplify.com/waseak/php/search-all-sightings.php?BBOX=-125.0,47.0,-122.0,49.5&start=2026-05-13&end=2026-05-20&species=Orcinus+orca&source=whale_alert
```

**Notes:**
- The `species=Orcinus+orca` filter catches all common-name variants: `Killer Whale (Orca)`, `Orca`, `Killer Whale`, and `Southern Resident Killer Whale` — all share the same scientific name.
- For the highest-quality sightings only, add `&trusted=1` (Cascadia Research, Orca Network, and PSWS observers are trusted).
- To extend coverage into Canadian waters (BC / Gulf Islands / southern Salish Sea), push the north edge to `50.5` and the west edge to `-126.0`.

**Sample response (2026-05-13 → 2026-05-20):**
```json
{
  "count": 64,
  "results": [
    {
      "id": 247095,
      "name": "Killer Whale (Orca)",
      "scientific_name": "Orcinus orca",
      "number_sighted": 8,
      "latitude": 47.97762,
      "longitude": -122.67776,
      "created": "2026-05-20 17:57:00",
      "comments": "[Orca Network] Biggs T65As and T64Bs northbound just off Olele Point",
      "moderated": 1,
      "trusted": 1,
      "source": "whale_alert"
    }
  ]
}
```

---

## 3. Any Whales in Alaska

**BBOX:** `-180.0, 54.0, -130.0, 72.0`  
(All of coastal Alaska from the Aleutians to the North Slope)

```
https://maplify.com/waseak/php/search-all-sightings.php
  ?BBOX=-180.0,54.0,-130.0,72.0
  &start=2026-05-13
  &end=2026-05-20
  &source=whale_alert
```

**One-liner:**
```
https://maplify.com/waseak/php/search-all-sightings.php?BBOX=-180.0,54.0,-130.0,72.0&start=2026-05-13&end=2026-05-20&source=whale_alert
```

**Notes:**
- No `species` filter — returns all species (Humpback, Killer Whale, Fin Whale, Sei, Minke, Beluga, etc.).
- The western edge uses `-180.0` to capture the Aleutian chain. The Aleutians span the antimeridian; sightings from the western islands will appear at negative longitudes.
- Add `&moderated=1` to restrict to confirmed sightings and reduce noise (`Unspecified` and `Other` entries are common from opportunistic observers in this region).
- Active season is roughly May–October; expect higher volume June–August.

**Species breakdown — sample week (2026-05-13 → 2026-05-20, 98 total):**

| Species | Count |
|---|---|
| Humpback Whale | 55 |
| Killer Whale | 14 |
| Sei Whale | 3 |
| Finback Whale | 1 |
| Orca | 1 |
| Unspecified / Other | 22 |

---

## 4. Any Whales in Monterey Bay

**BBOX:** `-122.5, 35.9, -121.5, 37.0`  
(Covers Monterey Bay from Santa Cruz south to Point Sur)

```
https://maplify.com/waseak/php/search-all-sightings.php
  ?BBOX=-122.5,35.9,-121.5,37.0
  &start=2026-04-20
  &end=2026-05-20
  &source=whale_alert
```

**One-liner:**
```
https://maplify.com/waseak/php/search-all-sightings.php?BBOX=-122.5,35.9,-121.5,37.0&start=2026-04-20&end=2026-05-20&source=whale_alert
```

**Notes:**
- Monterey Bay sighting volume is highly seasonal. Peak months are April–November when upwelling brings prey close to shore; individual weeks may return 0 confirmed sightings.
- Using a 30-day window (rather than the default 2 days) is recommended for monitoring this area.
- For unconfirmed sightings from opportunistic observers (which add volume), add `&moderated=2` or omit `moderated` entirely.
- Common species: Humpback Whale, Blue Whale (summer/fall), Fin Whale, Gray Whale (winter/spring migration), Orca.

**Sample confirmed sightings — prior 30 days:**

| Date | Species | Count | Location |
|---|---|---|---|
| 2026-05-03 | Humpback | 1 | 36.3098, -122.4110 |
| 2026-04-27 | Humpback | 2 | 36.7069, -122.1190 |
| 2026-04-27 | Fin Whale | 1 | 36.7500, -121.9837 |
| 2026-04-27 | Humpback | 1 | 36.7052, -121.9830 |

---

## Bounding Box Quick Reference

| Area | West | South | East | North |
|---|---|---|---|---|
| San Francisco Bay | -122.6 | 37.4 | -121.9 | 38.1 |
| Puget Sound + Salish Sea | -125.0 | 47.0 | -122.0 | 49.5 |
| Salish Sea (extended, incl. BC) | -126.0 | 47.0 | -122.0 | 50.5 |
| Monterey Bay | -122.5 | 35.9 | -121.5 | 37.0 |
| California Coast (full) | -124.5 | 32.5 | -117.0 | 42.0 |
| Alaska (coastal) | -180.0 | 54.0 | -130.0 | 72.0 |
| Pacific Northwest (WA/OR) | -125.0 | 42.0 | -122.0 | 49.0 |
| US West Coast (full) | -130.0 | 30.0 | -115.0 | 50.0 |

---

## Scientific Names Reference

| Common Name | Scientific Name |
|---|---|
| Gray Whale | *Eschrichtius robustus* |
| Humpback Whale | *Megaptera novaeangliae* |
| Killer Whale / Orca | *Orcinus orca* |
| Blue Whale | *Balaenoptera musculus* |
| Fin Whale / Finback Whale | *Balaenoptera physalus* |
| Sei Whale | *Balaenoptera borealis* |
| Minke Whale | *Balaenoptera acutorostrata* |
| Right Whale | *Eubalaena glacialis* (N. Atlantic) / *Eubalaena japonica* (N. Pacific) |
| Beluga | *Delphinapterus leucas* |
| Harbor Porpoise | *Phocoena phocoena* |
| Bottlenose Dolphin | *Tursiops truncatus* |

The `species` parameter does a hard equality match (`=`) on `scientific_name`.  
The `q` parameter does a loose full-text match — useful for partial names or when the scientific name is uncertain.
