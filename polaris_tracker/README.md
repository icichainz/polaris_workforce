# polaris_tracker

Live presence and movement traces for Polaris Workforce: which workers and foremen are on
which site and in which zone, right now and over the day. Written in Gleam, runs on the
BEAM.

- Design: [`docs/presence-tracking.md`](../docs/presence-tracking.md)
- API contract: [`docs/api/tracker-openapi.yaml`](../docs/api/tracker-openapi.yaml)
- Django's side: [`docs/api/presence-django-openapi.yaml`](../docs/api/presence-django-openapi.yaml)

**Status: hardened (P1).** Token claims v2, SQLite persistence for tracks and the outbox,
the sessions endpoint for Django's nightly reconciliation, and ingest that scales past
one process. Live presence state is still in memory by design (it rebuilds from the next
sightings after a restart). See [Status](#status).

## Run

```sh
gleam test                            # 84 tests: state machine, geo, tokens, wire, store, tz, end to end
gleam format --check src test
TRACKER_DEBUG=1 gleam run             # http://127.0.0.1:4100, dev secret, events logged
```

`gleam run` stores tracks and the outbox in `var/tracker.sqlite3` (ignored by git); set
`TRACKER_DB_PATH=:memory:` for a throwaway run.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRACKER_SHARED_SECRET` | required unless `TRACKER_DEBUG=1` | HS256 secret shared with Django |
| `TRACKER_PORT` | `4100` | Listening port |
| `TRACKER_BIND` | `127.0.0.1` | Interface to bind |
| `DJANGO_EVENTS_URL` | unset: log events only | `…/api/v1/presence/events/` |
| `TRACKER_CORS_ORIGIN` | unset | Dashboard origin, when served from another origin |
| `TRACK_RETENTION_DAYS` | `30` | Must match Django's `track_retention_days` setting |
| `TRACKER_DB_PATH` | `var/tracker.sqlite3` (relative to the working directory) | The tracker's own SQLite file for tracks and the outbox; `:memory:` keeps them in memory (lost on restart). The Docker image sets `/data/tracker.sqlite3`. |
| `TRACKER_ZONEINFO_DIR` | `/usr/share/zoneinfo` | Compiled IANA zones, for the `tz` of the sessions endpoint (fixed offsets need none) |

**Gleam version.** Gleam ≥ 1.14 is required (`gleam = ">= 1.14.0"` in `gleam.toml`);
built and tested with **Gleam 1.19.2 on Erlang/OTP 28**. The old pins for Gleam 1.12
are gone: mist 6.0 / glisten 9, `gleam_stdlib` 1.0, `gleam_json` 3.1, `gleeunit` 1.11,
`sqlight` 1.2 / `esqlite` 0.10 (exact versions in `manifest.toml`). Gleam 1.12 can no
longer build the project. On the dev machine the 1.19.2 release binary lives in
`~/.local/opt/gleam-1.19.2/` (Homebrew's `gleam` formula would also upgrade Erlang to
OTP 29); put it first on `PATH` or call it by full path.

**C compiler.** `sqlight` builds esqlite's bundled SQLite as a NIF on first build: it
needs a C compiler and `make` (Xcode command-line tools on macOS, `build-base` on
Alpine; the Dockerfile installs it in the build stage).

## Layout

| Module | Role |
| --- | --- |
| `presence` | Pure state machine: sightings → `Entered` / `MovedZone` / `Left`; `replay` |
| `geo` | Haversine distance, geofence inside / outside / uncertain |
| `token` | JWT HS256 verify and mint (claims v2), site and company scoping |
| `domain`, `wire` | Shared types; JSON decoders and encoders |
| `hub` | Ingest, run in each request's process: dedupe, geofences, track points, signals to site actors. Its actor only starts and watches site actors and prunes dedupe keys |
| `registry` | Shared ingest state in ETS: site configs, running site actors, dedupe keys |
| `site` | One actor per site, holding everyone's presence state, with a timer |
| `db` | The SQLite file: WAL, prepared statements, short transactions |
| `track_store` | Raw points in SQLite, one transaction per batch, hourly retention prune |
| `sessions`, `tz` | Sessions recomputed from the track; local days in an IANA zone or offset |
| `broadcaster` | Live events to SSE subscribers, per site |
| `outbox`, `django` | Events in SQLite until Django answers 2xx; batches, backoff |
| `api` | JSON endpoints (testable without sockets) |
| `server` | mist glue and the SSE stream |
| `polaris_tracker` | Config and the supervision tree |

## Tokens (claims v2)

Every token is HS256, header `{"alg":"HS256","typ":"JWT"}`, compact JSON, base64url without
padding. Claims, in the order Django writes them: `sub`, `kind`, `company` (every kind
except `service`; a token without one is refused), `sites` (explicit, sorted; `service`
reaches every site), `travel` (may see off-site GPS points of its own company), `aud`,
`exp`. `all_sites` is no longer read. GPS fixes are stored with the device token's
company; off-site points go only to `travel` tokens of that company.

The `kind` keeps each token to its endpoints (every kind shares the secret and
`aud = polaris-tracker`): only `service` reaches `/internal/*`, only `device` posts
`/v1/observations`, only `gateway` posts `/v1/gateways/sightings`; anything else is
`403 forbidden`.

**Foremen and consent.** Each site config lists, in `foremen`, the foremen who may be
tracked there (assigned, current consent notice accepted; missing means nobody). A GPS
fix inside a site that lists the foreman is stored for that site and feeds it; inside a
site of the token that does not list him it is dropped; off site it is stored as travel
only if some site of the token lists him. Dropped fixes are answered as `not_tracked` and
never stored. A new config without the foreman applies to the next fix.

The tracker mints one kind of token, for Django: `{"sub":"service:tracker",
"kind":"service","aud":"polaris-django","exp":<now + 300>}`. Tests check `token.mint`
byte for byte against tokens from Django's test suite (`KNOWN_DEVICE_TOKEN`,
`KNOWN_TRACKER_SERVICE_TOKEN` in `polaris_workforce/tests/test_presence_tokens.py`).

## Storage

One SQLite file (`TRACKER_DB_PATH`), never Django's, in WAL mode with
`synchronous = NORMAL` and a 5 s busy timeout. The track store and the outbox each hold
their own connection; statements are prepared once (a small FFI over esqlite, since
sqlight re-prepares on every call); every write is one short transaction.

- **Track points:** one row per point, indexed by person, by site and by time. A batch
  of sightings is one transaction. Every hour, points older than `TRACK_RETENTION_DAYS`
  are deleted in chunks of 5,000 rows.
- **Outbox:** one row per presence event (`event_id` unique). Rows are deleted only
  after Django answered 2xx; failures back off from 5 s up to 60 s; a full batch is
  followed at once by the next one.
- Back up the `-wal` and `-shm` files with the main file, or copy only while the tracker
  is stopped.

Dedupe keys (`client_uuid`s, gateway batch ids) are kept in memory for 48 h and are lost
on a restart, so a phone or gateway that resends after a tracker restart can store a
point twice; in a replay such a twin can count twice towards `sightings_to_enter`.
Persisting the keys (or a unique key per point) is left for later.

## Sessions for reconciliation

`GET /internal/sites/{site_id}/sessions?date=YYYY-MM-DD&tz=Africa/Lome` (service token)
replays the stored track of one local day through `presence.replay` with the site's
current rules and geofence. The local day runs from the first instant whose local date is
`date` to the first instant of the next local date (23 or 25 h on DST changes; when
midnight is skipped, from the transition). `tz` is an IANA name (read from
`TRACKER_ZONEINFO_DIR`, footer rules included) or an offset such as `%2B01:00`.
Response and rules in the OpenAPI.

## Load test

`bench/load_test.py` (standard library only, not run by `gleam test`) configures sites,
gateways and badges, mints gateway tokens with `hmac`, and sends BLE sightings over real
HTTP on an open-loop schedule (request `i` is due at `start + i / rate`, whatever happened
before). It reports latency percentiles and errors, and with `--db` how far the track
store lagged behind.

```sh
TRACKER_DEBUG=1 TRACKER_DB_PATH=/tmp/load.sqlite3 gleam run    # in one terminal
python3 bench/load_test.py --rate 500 --duration 60 --batch 1 --db /tmp/load.sqlite3
```

Results on an Apple M4 (10 cores, macOS 15.6, OTP 28, dev build), client on the same
machine; 10 sites, 20 gateways, 500 badges, file-backed SQLite. "service" is measured
from the moment the request is written to the full response:

| Run | Load | Requests | Errors | p50 | p95 | p99 | max | Store lag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | 500 sightings/s, 1 per request (500 req/s), 60 s | 30,000 | 0 | 0.28 ms | 0.64 ms | 1.76 ms | 43 ms | 0 s |
| A2 | same, again | 30,000 | 0 | 0.21 ms | 0.46 ms | 0.96 ms | 79 ms | 0 s |
| B | 500 sightings/s, 25 per request (20 req/s), 60 s | 1,200 | 0 | 0.62 ms | 1.75 ms | 2.55 ms | 59 ms | 0 s |
| C | 2,000 req/s, 1 per request, 30 s | 60,000 | 0 | 0.40 ms | 2.83 ms | 39 ms | 435 ms | 0.6 s |
| D | 20,000 sightings/s, 25 per request (800 req/s), 30 s | 24,000 | 0 | 0.33 ms | 1.05 ms | 3.49 ms | 56 ms | 1.1 s |
| E | 2,000 req/s, 60 s, past 200,000 dedupe keys | 120,000 | 0 | 0.39 ms | 1.63 ms | 19 ms | 187 ms | 0.1 s |

The tail at 2,000 req/s (C, E) is mostly the Python client (64 threads under the GIL).

**Why ingest left the hub.** The first version routed every batch through one hub
actor. Its rate was fine (run D's load passed through it at p50 0.4 ms), but its
in-memory dedupe table was pruned only at 200,000 keys, and only of keys older than 48 h:
past that size every batch re-filtered the whole table. Run E on that version collapsed:
p95 3.3 s, p99 5.0 s, 3,284 errors (HTTP 500 from call timeouts) and a crashed hub. A
busy deployment (10 gateway batches/s) would reach 200,000 keys in under 6 hours. Ingest
now runs in each request's process against ETS (`registry`); dedupe is an atomic
`insert_new`, pruned every 10 minutes.

**Next limits.** The track store is one writer: raw esqlite manages about 25,000–40,000
indexed rows/s on this machine (12 binds and a step per row), and the store kept up with
20,000 sightings/s with about a second of lag. The outbox writes one transaction per
published change, so a storm of zone changes (badges flapping between two gateways)
competes with the track store for the write lock. Dedupe keys cost memory: about 1.7
million keys over 48 h at 10 batches/s.

## Status

| Area | State |
| --- | --- |
| Presence state machine + replay (`presence`) | Built, unit-tested |
| Geofence maths (`geo`) | Built, unit-tested |
| JWT claims v2, verify and mint (`token`) | Built; tested byte for byte against Python stdlib and Django's test tokens; two-company isolation tested end to end |
| JSON decoding of all payloads (`wire`) | Built, unit-tested; badge ids trimmed and lower-cased |
| Ingest (`hub`, `registry`) | Built, off the single process; concurrent first touch starts one site actor; tested |
| Foremen consent (`SiteConfig.foremen`) | Built: unlisted foremen's fixes dropped as `not_tracked`, on and off site; withdrawal applies to the next fix; tested |
| Site actors, broadcaster (`site`, `broadcaster`) | Built; covered by end-to-end tests through the API |
| HTTP endpoints (`api`) and SSE (`server`) | Built; smoke-tested over real HTTP with tokens minted by Python's `hmac` |
| SQLite persistence for tracks and outbox (`db`, `track_store`, `outbox`) | Built: WAL, prepared statements, short transactions, hourly retention; restart tested (kill the actor, reopen, read back) |
| Outbox to Django (`outbox`, `django`) | Built: deleted only after 2xx, backoff to 60 s, drains long queues |
| Sessions for reconciliation (`sessions`, `tz`) | Built: `GET /internal/sites/{id}/sessions`; local days in IANA zones (TZif + POSIX footer) or offsets, checked against Python's `zoneinfo` |
| Load test (`bench/load_test.py`) | 500 sightings/s for 60 s with 0 errors, p99 < 3 ms; headroom to 20,000 sightings/s |
| Supervision | Built: `RestForOne` (broadcaster → outbox → track store → hub → site factory → HTTP); shared ingest state belongs to the caller of `start`, so it survives restarts inside the tree |
| Gleam version | Gleam 1.19.2, mist 6, stdlib 1.0 (≥ 1.14 required) |
| Docker image | Updated (Gleam 1.19.2 image, `build-base`, `tzdata`, `/data` volume); not built here (no Docker daemon) |
| Not built | Persistent dedupe keys; live presence state across restarts (by design) |

## Try it by hand

Tokens come from Django in production. For local testing, mint one with the dev secret.
This is the same HS256 JWT (claims v2) Django issues, and byte for byte what the
tracker's own `token.mint` produces for the same claims:

```python
import base64, hashlib, hmac, json, time
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
dumps = lambda o: json.dumps(o, separators=(",", ":")).encode()
def mint(sub, kind, company=0, sites=(), travel=False,
         secret=b"dev-only-insecure-tracker-secret", aud="polaris-tracker"):
    claims = {"sub": sub, "kind": kind}
    if kind != "service":
        claims |= {"company": company, "sites": sorted(set(sites)), "travel": travel}
    claims |= {"aud": aud, "exp": int(time.time()) + 3600}
    h, p = b64(dumps({"alg": "HS256", "typ": "JWT"})), b64(dumps(claims))
    return f"{h}.{p}.{b64(hmac.new(secret, f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
print(mint("service:django", "service"))
print(mint("user:1", "dashboard", company=1, sites=[3], travel=True))
```

Then push a site config (`PUT /internal/sites/3/config`), send gateway sightings or phone
observations, and watch `GET /v1/sites/3/live?access_token=<dashboard token>` with
`curl -N`. The request bodies are in the OpenAPI examples.
