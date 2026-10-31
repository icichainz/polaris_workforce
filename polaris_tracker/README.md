# polaris_tracker

Live presence and movement traces for Polaris Workforce: which workers and foremen are on
which site and in which zone, right now and over the day. Written in Gleam, runs on the
BEAM.

- Design: [`docs/presence-tracking.md`](../docs/presence-tracking.md)
- API contract: [`docs/api/tracker-openapi.yaml`](../docs/api/tracker-openapi.yaml)
- Django's side: [`docs/api/presence-django-openapi.yaml`](../docs/api/presence-django-openapi.yaml)

**Status: prototype.** Live state, tracks and the outbox to Django are kept in memory
(see the spec, §12).

## Run

```sh
gleam test                            # 46 tests: state machine, geo, tokens, wire, end to end
gleam format --check src test
TRACKER_DEBUG=1 gleam run             # http://127.0.0.1:4100, dev secret, events logged
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRACKER_SHARED_SECRET` | required unless `TRACKER_DEBUG=1` | HS256 secret shared with Django |
| `TRACKER_PORT` | `4100` | Listening port |
| `TRACKER_BIND` | `127.0.0.1` | Interface to bind |
| `DJANGO_EVENTS_URL` | unset: log events only | `…/api/v1/presence/events/` |
| `TRACKER_CORS_ORIGIN` | unset | Dashboard origin, when served from another origin |
| `TRACK_RETENTION_DAYS` | `30` | Must match Django's `track_retention_days` setting |

**Gleam version.** Dependencies are pinned to releases that build with Gleam 1.12 (the
version on the dev machine and in CI): mist 5 / glisten 8, `gleam_stdlib` < 0.68,
`gleam_json` 3.0, `exception` 2.1.0, `gleeunit` 1.6. After upgrading Gleam to 1.14 or
later, relax those pins in `gleam.toml` and move to mist 6.

## Layout

| Module | Role |
| --- | --- |
| `presence` | Pure state machine: sightings → `Entered` / `MovedZone` / `Left`; `replay` |
| `geo` | Haversine distance, geofence inside / outside / uncertain |
| `token` | JWT HS256 verify and mint, site scoping |
| `domain`, `wire` | Shared types; JSON decoders and encoders |
| `hub` | Routes observations, dedupes `client_uuid`s, checks geofences, starts site actors |
| `site` | One actor per site, holding everyone's presence state, with a timer |
| `track_store` | Raw points per person, with retention |
| `broadcaster` | Live events to SSE subscribers, per site |
| `outbox`, `django` | Batches events to Django and retries until accepted |
| `api` | JSON endpoints (testable without sockets) |
| `server` | mist glue and the SSE stream |
| `polaris_tracker` | Config and the supervision tree |

## Try it by hand

Tokens come from Django in production. For local testing, mint one with the dev secret.
This is the same HS256 JWT Django will issue:

```python
import base64, hashlib, hmac, json, time
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def mint(sub, kind, sites, secret=b"dev-only-insecure-tracker-secret"):
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": sub, "kind": kind, "sites": sites,
                        "aud": "polaris-tracker", "exp": int(time.time()) + 3600}).encode())
    return f"{h}.{p}.{b64(hmac.new(secret, f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
print(mint("service:django", "service", []))
```

Then push a site config (`PUT /internal/sites/3/config`), send gateway sightings or phone
observations, and watch `GET /v1/sites/3/live?access_token=<dashboard token>` with
`curl -N`. The request bodies are in the OpenAPI examples.
