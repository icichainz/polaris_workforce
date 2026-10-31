# Polaris Workforce — presence and movement tracking (design spec)

**Goal:** know, in real time, which workers and foremen are on which site and in which
zone, and keep a trace of their movements during working hours: arrival, zone changes,
departure and, for foremen, trips between sites. Presence **suggests** attendance in the
roll call; the foreman still confirms it, and attendance and pay keep their existing
approval and audit flow.

**Status:** design written, Gleam service prototyped in `polaris_tracker/` (see §12).
This is a new track that starts after Wave 3 of `docs/implementation-plan.md`.

**Contracts**

- Tracker service API: `docs/api/tracker-openapi.yaml`
- Django additions for this feature: `docs/api/presence-django-openapi.yaml` (to be
  merged into `docs/api/openapi.yaml` by the API owner once F3 has landed)

---

## 1. How people are detected

Workers do not carry the foreman app, and many have no smartphone. So the two groups are
detected differently.

| Method | Who | What it gives | Notes |
| --- | --- | --- | --- |
| **Phone GPS** (background, during the shift) | Foremen | A route: arrival on and departure from each site, trips between sites | `flutter_background_geolocation` (paid licence for Android release builds) or `geolocator` + OS geofences. Accuracy 10–50 m, worse inside concrete. |
| **BLE badge** on a lanyard or helmet, seen by **fixed gateways** | Workers | Zone-level presence: entry, zone changes, exit | One gateway at the gate, one per key zone. Gateways need power and an uplink (site 4G router) and must buffer sightings while offline. |
| **BLE badge** seen by the **foreman's phone** | Workers | Site-level presence (no zone) | The phone scans while the app is open (`flutter_blue_plus`). Covers sites with no gateway. |
| **QR / NFC badge scan** | Workers (fallback) | Check-in / check-out only | Forgotten badge, dead gateway. Goes through the existing roll call, not the tracker. |

GPS cannot tell zones apart inside a site; BLE can. Between sites, the foreman's GPS
route covers travel. A worker's own phone (a "worker app") is out of scope for now.

---

## 2. Architecture

```
 BLE gateways ───── POST /v1/gateways/sightings ─────┐
 Foreman phones ─── POST /v1/observations ───────────┤  (GPS fixes + BLE sightings)
                                                     ▼
                ┌──────────────── polaris_tracker (Gleam, BEAM) ────────────────┐
                │  http          auth (JWT HS256), JSON decoding, routing        │
                │  hub           site_id → site actor; GPS → geofence checks     │
                │  site actor    one per site: presence state per person,        │
                │                timer ticks, emits presence events              │
                │  track store   raw points per person, retention                │
                │  broadcaster   live events to SSE subscribers, per site        │
                │  outbox        batches events to Django, retries until 2xx     │
                └──────┬───────────────────────┬──────────────────────┬──────────┘
                       │ SSE /v1/sites/{id}/live│ GET …/track           │ POST /api/v1/presence/events/
                       ▼                        ▼                       ▼
                 office dashboard (htmx/Alpine, Leaflet map)          Django
                                                        PresenceSession, AuditLog,
                 PUT /internal/sites/{id}/config ◄───── site config, settings, tokens,
                                                        roll-call suggestions
```

**Who owns what**

| Concern | Owner | Why |
| --- | --- | --- |
| Sensing (GPS, BLE scan) | Flutter app, gateways | Only the device can sense. |
| Live state, timers, debouncing, fan-out, raw tracks | **Gleam tracker** | Many long-lived connections, a timer per person, state per site, crash isolation per site. |
| Sites, zones, gateways, badges, settings, permissions | Django | Already the source of truth; pushes config to the tracker. |
| Presence sessions, audit, attendance suggestions | Django | Durable records live in Django; attendance rules stay there. |

**Hard rules**

1. The tracker **never writes to Django's database**. It keeps its own state and its own
   storage, and talks to Django only over HTTP.
2. Presence **never marks attendance**. Django turns sessions into roll-call
   *suggestions* that the foreman confirms.
3. Thresholds and geofences are **settings**, resolved in Django through
   `resolve_setting` and pushed to the tracker in the site config. The tracker has no
   business defaults of its own. The one exception is track retention, a company-wide
   setting the tracker reads from `TRACK_RETENTION_DAYS` at deploy time.

---

## 3. Identities and tokens

Django issues every token. All tokens are **JWT HS256**, signed with a secret shared by
Django and the tracker (`TRACKER_SHARED_SECRET`). The tracker verifies the signature,
`aud`, `exp` and `kind`.

| `kind` | `sub` | Issued to | Lifetime | Can do |
| --- | --- | --- | --- | --- |
| `device` | `foreman:<user_id>` | Foreman phone, via `POST /api/v1/tracking/token/` | 12 h | Send observations; read presence of its sites |
| `gateway` | `gateway:<gateway_id>` | A fixed gateway, from the office screen | Long (1 year); revoked by removing the gateway from the site config | Send sightings for its site |
| `dashboard` | `user:<user_id>` | Office user, embedded in the page by Django | 1 h, refreshed by the page | Read presence, live stream, tracks |
| `service` | `service:django` / `service:tracker` | Server to server | 5 min, minted per call | Push site config (Django → tracker); post events (tracker → Django) |

Claims: `aud = "polaris-tracker"` (or `"polaris-django"` for tokens the tracker sends to
Django), `sub`, `kind`, `sites` (list of site ids), `all_sites` (admins), `exp`.

**Site scoping (rule 8)** is enforced in the tracker on every endpoint: a token only
reaches the sites in its `sites` claim. Track points recorded **off site** (travel
between sites) are returned only to tokens with `all_sites = true`.

---

## 4. Subjects, observations and events

- **Subject:** `worker:<id>` (Django `Worker` pk) or `foreman:<id>` (Django `User` pk).
- **Observation** (input):
  - `gps`: subject from the token, `at`, `lat`, `lng`, `accuracy_m`.
  - `ble`: `badge_id`, `at`, `rssi`, plus the site (from the phone's payload, or from the
    gateway's config) and the zone (gateway only). The badge maps to a worker through
    the site config.
- Every phone observation carries a `client_uuid`; every gateway batch carries a
  `batch_id`. Repeats are acknowledged as duplicates and ignored (rule 7).
- **Presence event** (output): `entered`, `moved_zone`, `left` with `event_id`
  (UUID), `site_id`, `subject`, `at`, `zone_id`, `from_zone_id`, `reason`
  (`timeout` or `left_geofence`). Django stores them idempotently by `event_id`.

---

## 5. Presence state machine

Raw sightings flicker (BLE) and jump (GPS). The tracker turns them into clean events
with a pure function in `polaris_tracker/src/polaris_tracker/presence.gleam`, one state per (site, person):

```
            Seen                 Seen ×(sightings_to_enter)
   Away ─────────► Arriving ───────────────────────────────► OnSite ──┐ Seen in another zone
    ▲                 │ no confirmation within arrival_window   │  ▲   └─► MovedZone event
    │                 ▼                                         │  │
    └──────────── (back to Away, no event)                       │  └── Seen (refresh last_seen)
    ▲                                                           │
    └──── Left(timeout): no sighting for absence_to_leave ──────┤
    └──── Left(left_geofence): GPS fix clearly outside ─────────┘
```

| Rule (setting) | Meaning | Suggested default |
| --- | --- | --- |
| `presence_sightings_to_enter` | Sightings needed before `entered` fires | 2 |
| `presence_arrival_window_seconds` | Time allowed to collect them | 300 |
| `presence_absence_to_leave_seconds` | Silence before `left` fires | 900 |

- `entered` is dated at the **first** sighting; `left` at the **last** one, not at the
  moment the timeout is noticed.
- A sighting with no zone (phone BLE, GPS) keeps the current zone.
- Out-of-order data never moves `last_seen` backwards.
- **Late data.** Observations older than `absence_to_leave` are stored in the track but
  not fed to the live state (they would reopen closed sessions). `presence.replay` folds
  the same `step` function over a day's sorted observations to recompute sessions from
  the track; a nightly reconciliation (phase 3) uses it to correct Django's sessions.

### GPS and geofences

A site geofence is a centre and a radius (polygons later). For each GPS fix, for each
site in the token:

- `distance ≤ radius` → `Seen` on that site.
- `distance > radius + accuracy` → `SeenOutside` on that site (closes a session
  early with `left_geofence`).
- In between → ambiguous: stored in the track only.

---

## 6. Tracker API (summary)

Full contract with examples: `docs/api/tracker-openapi.yaml`.

| Method & path | Token | Purpose |
| --- | --- | --- |
| `GET /health` | none | Liveness and number of configured sites |
| `POST /v1/observations` | device | Batch of GPS fixes and BLE sightings (≤ 500) |
| `POST /v1/gateways/sightings` | gateway | Batch of BLE sightings from a gateway (≤ 1000) |
| `GET /v1/sites/{site_id}/presence` | device, dashboard | Who is on site now |
| `GET /v1/sites/{site_id}/live` | dashboard (header or `?access_token=`, since `EventSource` cannot set headers) | Server-sent events: a snapshot, then presence events, and a ping every 25 s |
| `GET /v1/people/{kind}/{id}/track?from=&to=` | dashboard | Raw track points (≤ 7 days) |
| `PUT /internal/sites/{site_id}/config` | service | Django pushes geofence, zones, gateways, badges, rules |

Errors use the same shape as the Django API: `{"code", "message", "details"}`.

---

## 7. Django side

New app **`presence`** (owns its models, migrations, services, API, templates):

- `Gateway` (site, zone, label, token issued at, revoked at), `Zone` (site, code,
  name), `Badge` (badge id, worker, active from/to), `PresenceEvent` (event id unique,
  site, subject kind/id, type, at, zones, reason), `PresenceSession` (site, subject,
  started at, ended at, zones visited), built from events in `services.py`.
- **API** (`docs/api/presence-django-openapi.yaml`):
  `POST /api/v1/tracking/token/` (phone gets a tracker token),
  `POST /api/v1/presence/events/` (tracker → Django, idempotent by `event_id`),
  `GET /api/v1/sites/{id}/presence-sessions/?date=` (roll-call suggestions).
- **Config push:** a service `push_site_config(site)` called when a site, zone, gateway,
  badge or a tracking setting changes (and on a periodic resync).
- **Requests for other areas:** `worksites` — geofence fields on `Site` (lat, lng,
  radius); `workers` — badge on the worker form; `core` — the settings below;
  `attendance` — show "seen 07:12–16:48, zone B" in the roll call and the approvals
  screen; `dashboard` — live map (Leaflet + OpenStreetMap, stored in `static/vendor/`)
  and a per-person timeline.

**Settings** (company → site → worker, through `resolve_setting`):

| Setting | Levels | Default |
| --- | --- | --- |
| `tracking_enabled` | company, site, worker | off |
| `presence_sightings_to_enter` | company, site | 2 |
| `presence_arrival_window_seconds` | company, site | 300 |
| `presence_absence_to_leave_seconds` | company, site | 900 |
| `geofence_radius_m` | company, site | 150 |
| `foreman_gps_interval_seconds` | company, site | 60 |
| `tracking_hours` (start, end) | company, site | 06:00–19:00 |
| `track_retention_days` | company | 30 |

A worker with `tracking_enabled = off` has no badge mapping pushed to the tracker, so
their sightings are dropped at ingest.

**Audit:** presence events are not attendance changes and do not need an `AuditLog`
entry each. Accepting a suggestion into the roll call does (rule 6), and so does every
change to tracking settings, badges and gateways.

---

## 8. Flutter side

- After PIN sign-in, call `POST /api/v1/tracking/token/` and keep the token in secure
  storage; refresh before `expires_at`.
- **GPS:** only inside `tracking_hours` and while the foreman is signed in; interval
  from `foreman_gps_interval_seconds`. Send OS geofence enter/exit as extra fixes.
  Reject mock locations on Android (`isMock`).
- **BLE:** scan while the app is open on a site screen; report each badge at most once
  per 30 s.
- **Offline:** observations go into the existing drift outbox with a `client_uuid` and
  their device timestamp, and are replayed in batches. The tracker handles late data
  (§5).
- **Consent screen** on first use, with the text from §10.

---

## 9. Storage and retention

| Data | Where | Retention |
| --- | --- | --- |
| Live presence state | Tracker memory (site actors) | Rebuilt from new sightings after a restart |
| Raw track points | Tracker's **own** store (prototype: memory; production: its own SQLite file via `sqlight`, separate from Django's) | `TRACK_RETENTION_DAYS` env var (must match Django's `track_retention_days`), pruned hourly |
| Presence events and sessions | Django database | Same as attendance |
| Outbox (events not yet accepted by Django) | Tracker memory (production: its own SQLite file) | Until Django returns 2xx |

Volume: 200 workers sighted every 30 s for 10 h ≈ 240,000 points a day. That is why raw
points stay out of Django's SQLite file.

---

## 10. Privacy and legal

Tracking employees' movements is regulated almost everywhere (GDPR and CNIL guidance in
France; national data-protection authorities in most francophone African countries).
Check the rules for each country before buying hardware. The design follows the common
requirements:

- Tracking only during `tracking_hours` and only for people with `tracking_enabled`.
- Clear notice and consent text (EN/FR), shown in the app and printed for badge holders.
- Raw tracks deleted after `track_retention_days`; sessions kept like attendance.
- Off-site travel visible to admins only (`all_sites`).
- A worker can ask to see their own presence history (export from the worker profile).
- Presence is used to suggest attendance, not to discipline automatically.

---

## 11. Failure modes

| Failure | Behaviour |
| --- | --- |
| Phone offline | Outbox replays later; late observations go to the track, the nightly replay fixes sessions. |
| Gateway offline | Gateway buffers; a site with no sightings for `absence_to_leave` will emit `left` for everyone, then `entered` again once data arrives. The reconciliation corrects the gap. |
| Tracker restart | Live state empties; people reappear as `Arriving` → `OnSite` on their next sightings. Outbox and tracks survive once they move to SQLite (production). |
| Django down | Outbox keeps events and retries with backoff. Live map keeps working. |
| One site actor crashes | Only that site is affected; the hub starts a fresh actor on the next observation. |
| GPS spoofing | Mock-location check on Android; cross-check foreman GPS against BLE where gateways exist. |

---

## 12. Prototype status (`polaris_tracker/`)

| Area | State |
| --- | --- |
| Presence state machine + replay (`presence.gleam`) | Built, unit-tested |
| Geofence maths (`geo.gleam`) | Built, unit-tested |
| JWT verification and minting (`token.gleam`) | Built, unit-tested |
| JSON decoding of all payloads (`wire.gleam`) | Built, unit-tested |
| Hub, site actors, track store, broadcaster (`hub.gleam`, `site.gleam`, `track_store.gleam`, `broadcaster.gleam`) | Built; covered by end-to-end tests through the API |
| HTTP endpoints (`api.gleam`) and SSE (`server.gleam`) | Built; smoke-tested over real HTTP with tokens minted by Python's `hmac` (the same scheme Django will use) |
| Outbox to Django (`outbox.gleam`) | Built: batches and retries; logs only when `DJANGO_EVENTS_URL` is unset |
| SQLite persistence for tracks and outbox | **Not built** (in memory). `sqlight` needs a C build and a ~9 MB download. |
| Supervision | Built: a `RestForOne` static supervisor (broadcaster → outbox → track store → hub → site factory → HTTP); site actors run under a factory supervisor and the hub monitors them. |
| Nightly reconciliation | Not built (`presence.replay` is ready for it) |
| Gleam version | Dependencies pinned to builds that work on Gleam 1.12 (mist 5, stdlib < 0.68). Upgrading Gleam to ≥ 1.14 and moving to mist 6 is part of P1. |

---

## 13. Work breakdown (after Wave 3)

| # | Brief | Owns | Done when |
| --- | --- | --- | --- |
| P1 | **Tracker hardening** | `polaris_tracker/` | SQLite persistence for tracks and outbox (its own file); Gleam ≥ 1.14 and mist 6; hub ingest moved off the single hub process if the load test needs it; load test of 500 sightings/s; `gleam test` and `gleam format --check` pass. |
| P2 | **Django `presence` app** | `apps/presence/` (+ its migrations), `docs/api/presence-django-openapi.yaml` | Models, token endpoint, idempotent event ingest, sessions from events, config push; site scoping tested; replaying the same events changes nothing. |
| P3 | **Settings, sites, workers changes** | Lead applies the requests from P2 in `core`, `worksites`, `workers` | Settings resolve at every level; geofence and badge fields on the forms. |
| P4 | **Dashboard: live map and timeline** | `apps/presence/templates/`, `static/vendor/leaflet/` | Map updates from SSE without reload; timeline per person; EN/FR; works at phone width. |
| P5 | **Roll-call suggestions** | `apps/attendance/` (agent C's area) | Suggestions shown in the roll call and approvals; accepting one writes an `AuditLog` entry. |
| P6 | **Flutter tracking** | `polaris_foreman/lib/tracking/` | Token, GPS, BLE scan, outbox, consent screen; works in airplane mode. |
| P7 | **Pilot** | Lead + site team | One site, 2–3 gateways, 20 badges for two weeks; compare sessions with the confirmed roll call. |

**Merge order:** P2 → P3 → P1 and P4 in parallel → P5 → P6 → P7.

---

## 14. Open questions

1. Badge hardware and gateway model (commercial gateway or ESP32 firmware), and who
   installs them.
2. Polygon geofences for long or irregular sites.
3. Whether site managers may see off-site travel of their own foremen.
4. Where the tracker is hosted (decided with I3, deployment).
