# Polaris Workforce — foreman API (v1)

The contract between the Django backend (`polaris_workforce/`) and the Flutter foreman
app (`polaris_foreman/`). The source of truth is **[`openapi.yaml`](openapi.yaml)**, which
drf-spectacular generates from the code. This page explains the conventions behind it.

- Base URL: `https://<server>/api/v1/`
- Every path ends with a slash: `/api/v1/me/`, not `/api/v1/me`.
- JSON in and out (`Content-Type: application/json`). The only exception is
  `POST /payments/`, which also accepts `multipart/form-data` for the proof file.
- Swagger UI, DEBUG only: `/api/v1/docs/`. The raw schema is at `/api/v1/schema/`
  (`?format=json` for JSON).

## Endpoints

| Method & path | Purpose | Success | Wave 2 owner |
| --- | --- | --- | --- |
| `GET /health/` | Is the server reachable? No auth. | 200 | done |
| `POST /auth/pin/` | Phone + PIN + device ID → device token. No auth; throttled. | 200 | A |
| `POST /auth/logout/` | Revoke this device's token | 204 | A |
| `GET /me/` | Profile, company (time zone, currency), assigned sites | 200 | A |
| `GET /sites/{site_id}/crew/?date=` | Workers expected on the site that day | 200 | B |
| `GET /sites/{site_id}/days/?from=&to=` | Day summaries (home screen "This week") | 200 | C |
| `GET /sites/{site_id}/days/{date}/` | The day and its records (`not_started` if none) | 200 | C |
| `PUT /sites/{site_id}/days/{date}/records/` | Batch upsert of records | 200 | C |
| `POST /sites/{site_id}/days/{date}/submit/` | Submit the roll call | 200 | C |
| `POST /records/{record_id}/corrections/` | Correct a record after submission (reason required) | 201 / 200 replay | C |
| `GET /sync/?since=` | Days, records and corrections changed since | 200 | C |
| `GET /sites/{site_id}/payouts/{date}/` | Cash sheet for the day | 200 | D |
| `POST /payments/` | Record a payment with proof | 201 / 200 replay | D |

Which screen uses what:

| Screen | Calls |
| --- | --- |
| PIN sign-in | `POST /auth/pin/`, then `GET /me/` |
| Today (home) | `GET /me/` (site, `roll_call_due`), `GET /sites/{id}/crew/` (crew size), `GET /sites/{id}/days/` (today's status, "This week" strip with counts and `payout_status`) |
| Roll call | `GET /sites/{id}/crew/`, `GET /sites/{id}/days/{date}/`, `PUT .../records/` |
| Review & submit | Day `counts`, crew `absent_streak`, `POST .../submit/` with `foreman_note` |
| Cash payout | `GET /sites/{id}/payouts/{date}/`, `POST /payments/` |
| Background | `GET /health/`, `GET /sync/` |

## Authentication: PIN → device token

1. On first launch the app makes a **device ID** (a UUID) and keeps it for as long as
   it is installed.
2. The foreman enters phone number + 4-digit PIN. The app calls
   `POST /auth/pin/ {"phone", "pin", "device_id", "device_name"}`.
3. The server answers `{"token", "device", "user", "sites"}`. The token is shown only
   once: keep it in `flutter_secure_storage`. Signing in again from the same device ID
   replaces the old token.
4. Every other call sends:

   ```
   Authorization: Device <token>
   ```

5. `401 not_authenticated` means the token is missing, wrong, or revoked (logout, or an
   admin revoked the device on the Users screen). Clear the token and show the PIN
   screen. **Keep the outbox**: send it after the foreman signs in again.

PIN errors:

- Wrong PIN: `401 not_authenticated`, with `details.attempts_left`.
- Too many wrong PINs: `423 locked`, with `details.locked_until` (ISO datetime).
- More than 10 tries a minute from one IP address: `429 throttled`, with
  `details.retry_after` (seconds; also sent in the `Retry-After` header).

The server stores only `sha256(token)` (`ForemanDevice.token_hash`) and updates
`last_seen_at` at most once a minute. Office staff can also call the API with their
browser session (session authentication; unsafe methods need CSRF), which is useful in
Swagger UI.

## Errors

Every error response, whatever the status, has this shape:

```json
{"code": "validation_error", "message": "Invalid input.", "details": {"reason": ["This field is required."]}}
```

- `code` never changes: **branch on it**.
- `message` is translated (send `Accept-Language: fr` or `en`). You can show it, but
  don't parse it.
- `details` is always an object.

| HTTP | `code` | When | `details` |
| --- | --- | --- | --- |
| 400 | `validation_error` | Bad body or query string | field → list of messages; nested for lists, e.g. `{"records": [{}, {"status": ["…"]}]}`; `non_field_errors` for the rest |
| 401 | `not_authenticated` | No token, a wrong token, a revoked token, a wrong PIN | wrong PIN: `attempts_left` |
| 403 | `permission_denied` | Signed in but not allowed, e.g. a site you are not assigned to | — |
| 404 | `not_found` | No such object | — |
| 405 | `method_not_allowed` | Wrong HTTP method | — |
| 409 | `conflict` | The request clashes with the current state (a `client_uuid` reused for another request, submitting with unmarked workers, paying a sheet that is not open) | context, e.g. `unmarked_worker_ids`, `client_uuid` |
| 415 | `unsupported_media_type` | Wrong `Content-Type` | — |
| 423 | `locked` | The day or pay period is locked or submitted, or the device is locked after failed PINs | e.g. `status`, `locked_until` |
| 429 | `throttled` | Too many requests | `retry_after` (seconds) |
| 500 | `server_error` | A bug on the server | — |
| 501 | `not_implemented` | The endpoint is in the contract but not built yet (Wave 1 stubs) | — |

One exception: a URL that matches no route at all (a typo, or an impossible date such
as `2026-02-30` in the path) gets Django's plain 404 page, not JSON. Treat any 404 as
`not_found`.

## Idempotency: `client_uuid`

The app saves every write to the phone first, with a **`client_uuid`** (UUID v4) made
when the foreman acts, then sends it from an outbox. It may send the same write several
times (timeouts, a lost response, a crash). The server applies it only once:

| Kind | Endpoints | First time | Repeat with the same `client_uuid` |
| --- | --- | --- | --- |
| Create | `POST /payments/`, `POST /records/{id}/corrections/` | `201` + the new object | `200` + the **original** object, header `Idempotent-Replayed: true` |
| Upsert | `PUT /sites/{id}/days/{date}/records/` | `200` + the day | `200` + the day; nothing changes on the server |
| Action | `POST /sites/{id}/days/{date}/submit/` | `200` + the day | `200` + the day |

Rules for the app:

- **One `client_uuid` per write you mean once.** For a payment or a correction, make it
  when the foreman taps Confirm, and reuse it for every retry of that write.
- **Attendance records:** make one `client_uuid` per worker per day, the first time the
  worker is marked, and reuse it for every later change to that record. The server finds
  records by `client_uuid`, then by `worker_id`. If the same worker was marked on two
  phones, the response carries the server's `client_uuid`: store that one in place of
  yours.
- `409 conflict` on a replay means the `client_uuid` was used for a different request.
  That is a bug in the app: don't retry it.
- Treat both `200` and `201` as success.

The server side of this is in `polaris_workforce/apps/api/idempotency.py`
(`run_once`, `apply_changes`, `IdempotentCreateMixin`).

## Sync: `since` and `server_time`

```
GET /api/v1/sync/?since=2026-10-31T07:45:00Z
→ {"server_time": "2026-10-31T16:12:30Z", "since": "…", "days": […], "records": […], "corrections": […]}
```

- On the first sync, leave out `since`: you get the last 14 days for every assigned site.
- Store `server_time` exactly as received, and send it as `since` next time. Don't use
  the phone clock: it may be wrong.
- `server_time` is read before the changes, so nothing is missed. The same item may come
  back twice, so apply every item as an **upsert by `id`**.
- **The server wins.** A day approved, rejected or locked in the office, or a record
  corrected there, replaces the phone's copy. Keep any unsent outbox writes and send
  them anyway: the server answers `423`/`409` when they are no longer allowed, and the
  app tells the foreman.
- `days[].payout_status` tells the app when the cash payout opens (`open`), when
  everyone has been paid (`paid`), and when the office has closed the sheet (`closed`).
- Times in `since`/`server_time` are ISO 8601 with a time zone offset; URL-encode `+`.

## Money

- **Money is always an integer in the currency's smallest unit, never a float.** Every
  response that contains money also has a `currency` object:

  ```json
  "currency": {"code": "XOF", "decimals": 0}
  ```

  For display, divide by `10^decimals`: XOF `5000` is "5 000 XOF"; EUR `1250` with
  `decimals: 2` is "12,50 €". Do the maths in integers.
- The payout sheet always adds up: `total_to_pay = Σ lines.net`,
  `left_to_pay = total_to_pay − paid_total`, `cash_in_hand = cash_issued − paid_total`.
- `units` (days or hours) is a decimal **string**, e.g. `"0.50"`.

## Dates and times

- Dates: ISO 8601 `YYYY-MM-DD`. A work day is a date in the company's time zone
  (`GET /me/` → `company.time_zone`), not the phone's.
- Times of day (arrival, departure, `work_start`, `roll_call_due`): `"HH:MM"`, 24-hour,
  site local time. `null` when not recorded.
- Timestamps (`submitted_at`, `paid_at`, `updated_at`, `server_time`): ISO 8601 with an
  offset.

## Enum values

The API uses words. The app shows letters.

| Field | Values |
| --- | --- |
| Record `status` | `present` (P), `absent` (A), `half_day` (½), `late` (L) |
| Day `status` | `not_started`, `draft`, `submitted`, `approved`, `rejected`, `locked` |
| `payout_status` / payout `status` | `not_open`, `open`, `paid`, `closed` |
| `proof_type` | `photo`, `signature`, `thumb_mark`, `none` |
| Correction `field` | `status`, `arrival`, `departure`, `note` |
| `role` | `admin`, `site_manager`, `foreman`, `accountant` |
| `pay_type` | `daily`, `hourly` |
| `language` | `en`, `fr` |

## Working offline (for the Flutter app)

- Read from the local database (drift) and write to it first. The network only moves
  the outbox and refreshes the cache.
- Before draining the outbox, call `GET /health/` (it is cheap). Then send writes **in
  order, one at a time**: records → submit → corrections → payments. A correction needs
  the server's `record_id`, so send it only after the records `PUT` has returned.
- Retry network errors and `5xx` with backoff. Don't retry `400`, `403`, `404`, `409` or
  `423`: show them to the foreman. On `401`, sign in again and keep the outbox. On `429`,
  wait `retry_after`.
- Batch the roll call: one `PUT .../records/` holds up to 200 records.
- The network can be as slow as ~15 KB/s. Cache the crew list and worker photos (they
  rarely change). Compress proof photos on the phone (JPEG, longest side ≤ 1280 px,
  well under the 2 MB limit). Send `paid_at` from the moment of payment, because an
  offline payment can reach the server hours later.
- `GET /sites/{id}/days/{date}/` always returns a day, with `status: not_started` and
  `id: null` when nothing has been saved yet.
- Until Wave 2 is merged, every endpoint except `/health/` answers
  `501 not_implemented`. Use the mock data, which follows the examples in
  `openapi.yaml`.

## Regenerating the contract

The schema is generated from the serializers and `@extend_schema` declarations in
`apps/<app>/api/`. After changing any of them, regenerate the file and commit it with
the code:

```sh
cd polaris_workforce
uv run python manage.py spectacular --file ../docs/api/openapi.yaml --validate
```

`tests/test_api_schema.py` regenerates the schema and fails if `openapi.yaml` is out of
date.

Where to change things:

- Endpoints, serializers and examples: `apps/<app>/api/` (the Wave 2 owner of that app).
- Shared enums, the error format, money and time fields, authentication, idempotency:
  `apps/api/`. These are contract changes, so coordinate with the lead.
- Routes: each app's `api/urls.py`. `apps/api/urls.py` includes them all, once each.
