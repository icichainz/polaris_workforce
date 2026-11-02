# Presence tracking — open decisions and follow-ups

Written 2026-11-02, when briefs P1–P6, the nightly reconciliation and the review fixes were
merged into `main` (see `docs/presence-tracking.md` §12 and `docs/implementation-plan.md`
§14). Nothing here blocks the code that exists; each item needs an owner decision or a
small follow-up brief.

## 1. Decisions for the owner

| # | Decision | Why it matters | Current state / default |
| --- | --- | --- | --- |
| D1 | **Badge and gateway hardware** (commercial gateway or ESP32 firmware; badge model and frame format; who installs) | The pilot (P7) cannot start without it. **iPhones cannot see iBeacon frames** in a Bluetooth scan, so if foremen use iPhones the badges must also advertise another frame (e.g. Eddystone or service data). | The phone decodes iBeacon only, as `ib:<uuid>:<major>:<minor>`, in one replaceable function (`polaris_foreman/lib/tracking/badge_decoder.dart`). Badge ids are opaque, lower-cased strings everywhere else. |
| D2 | **CLAUDE.md tracker line**: change "Gleam 1.12" to "Gleam ≥ 1.14 (tested on 1.19.2)" | The tracker no longer builds on 1.12. The `gleam` on this machine's PATH (Homebrew) is still 1.12; 1.19.2 is installed at `~/.local/opt/gleam-1.19.2/gleam`. `brew upgrade gleam` would also upgrade Erlang 28 → 29 machine-wide. | Not changed: waiting for approval. |
| D3 | **Off-site travel for site managers**: may a site manager see their own foremen's trips between sites? | Privacy (design §10). | Company admins only (the `travel` token claim). |
| D4 | **Polygon geofences** for long or irregular sites | Circles misjudge long sites (roads, bridges). | Circles only (`checkin.SiteGeofence`, centre + radius). |
| D5 | **Admins on a phone**: should an admin's GPS be tracked on sites they are not assigned to as foreman? | An admin's phone gets a tracker token for every company site, but the tracker drops their GPS unless they are in a site's `foremen` list (assigned foremen with consent). | Admins are not tracked unless assigned to the site. |
| D6 | **Hosting** for the tracker (and the whole platform) | Needed for HTTPS, real phones and the pilot. | Deferred (also open for go-live, `docs/implementation-plan.md` §11). |

## 2. Follow-ups (code)

| # | Follow-up | Area | Notes |
| --- | --- | --- | --- |
| F1 | **Site configs are lost when the tracker restarts**: it then refuses all data until Django's next resync (up to 10 minutes). | `polaris_tracker/` (and/or `apps/presence/`) | Store configs in the tracker's SQLite file, or have Django push all configs again when the tracker comes back (e.g. the resync watching `/health`). Found in the live smoke test. |
| F2 | **Persistent dedupe keys** in the tracker: a resend after a tracker restart can store a point twice. | `polaris_tracker/` | Keys are in memory only. |
| F3 | **Phone smoke tool** fails on demo data (`423 locked`: `seed_demo` already submitted today's roll call) and then crashes with a null check instead of reporting. | `polaris_foreman/tool/live_smoke.dart`, `seed_demo` | The tracking steps of the smoke test pass. |
| F4 | **OS geofence enter/exit fixes** on the phone (design §8) are not built: tracking starts the next time the app is opened if tracking hours begin while it is in the background. | `polaris_foreman/lib/tracking/` | Needs a geofencing plugin (`geolocator` has none). |
| F5 | **A worker whose own tracking override is on at a site where tracking is off** does not trigger the phone's presence-suggestion fetch. | API + phone | Narrow; closing it needs a site flag in the API. |
| F6 | **Company deactivation is not audited** (`CompanyAdmin`), so it pushes nothing at once; the 10-minute resync turns tracking off. | `apps/core/admin.py` | |
| F7 | **Per-site push locks** use the file-based cache ("add if missing"): fine on one host, not atomic across hosts. | `config/settings.py` | Use Redis or Memcached for the cache if Celery workers ever run on several hosts. Run the worker with the default (prefork) pool so task time limits apply. |
| F8 | `seed_demo` logs a full traceback when the tracker is unreachable during the config push. | `apps/presence/services.py` | Cosmetic: log a one-line warning instead. |
| F9 | `docs/api/README.md` header still says "contract 1.1.0" (the contract is 1.5.0). | docs | |

## 3. Not verified yet

- Docker images built and run (`docker compose up --build`): no Docker daemon on the dev machine.
- The Android release build of the foreman app (R8 keep rule for `flutter_blue_plus`, new permissions) and the phone database upgrade (sqflite schema 2 → 3) on a real device.
- BLE scanning with real badges; GPS in the background on real phones.
- The pilot itself (P7): one site, 2–3 gateways, 20 badges for two weeks, sessions compared with the confirmed roll call.
