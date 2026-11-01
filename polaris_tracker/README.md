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

**Gleam version.** Gleam ≥ 1.14 is required (`gleam = ">= 1.14.0"` in `gleam.toml`);
built and tested with **Gleam 1.19.2 on Erlang/OTP 28**. The old pins for Gleam 1.12
are gone: mist 6.0 / glisten 9, `gleam_stdlib` 1.0, `gleam_json` 3.1, `gleeunit` 1.11
(exact versions in `manifest.toml`). Gleam 1.12 can no longer build the project. On the
dev machine the 1.19.2 release binary lives in `~/.local/opt/gleam-1.19.2/` (Homebrew's
`gleam` formula would also upgrade Erlang to OTP 29); put it first on `PATH` or call it by
full path.

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
This is the same HS256 JWT (claims version 2) Django will issue, and byte for byte what
the tracker's own `token.mint` produces for the same claims:

```python
import base64, hashlib, hmac, json, time
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
dumps = lambda o: json.dumps(o, separators=(",", ":")).encode()
def mint(sub, kind, company=0, sites=(), travel=False,
         secret=b"dev-only-insecure-tracker-secret", aud="polaris-tracker"):
    claims = {"sub": sub, "kind": kind}
    if company: claims["company"] = company        # every kind except service
    if sites: claims["sites"] = list(sites)        # explicit; service reaches all
    if travel: claims["travel"] = True             # off-site points of `company`
    claims |= {"aud": aud, "exp": int(time.time()) + 3600}
    h, p = b64(dumps({"alg": "HS256", "typ": "JWT"})), b64(dumps(claims))
    return f"{h}.{p}.{b64(hmac.new(secret, f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
print(mint("service:django", "service"))
print(mint("user:1", "dashboard", company=1, sites=[3], travel=True))
```

Then push a site config (`PUT /internal/sites/3/config`), send gateway sightings or phone
observations, and watch `GET /v1/sites/3/live?access_token=<dashboard token>` with
`curl -N`. The request bodies are in the OpenAPI examples.
