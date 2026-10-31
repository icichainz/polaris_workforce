# Polaris Workforce — foreman API (v1, contract 1.1.0)

The contract between the Django backend (`polaris_workforce/`) and the Flutter foreman
app (`polaris_foreman/`). The source of truth is **[`openapi.yaml`](openapi.yaml)**, which
drf-spectacular generates from the code. This page explains the conventions behind it.
The URL prefix stays `/api/v1/`; the contract version (`info.version`) is `1.1.0`. What
changed since 1.0 is listed in [Changes in 1.1](#changes-in-11).

- Base URL: `https://<server>/api/v1/`
- Every path ends with a slash: `/api/v1/me/`, not `/api/v1/me`.
- JSON in and out (`Content-Type: application/json`). The only exception is
  `POST /payments/`, which also accepts `multipart/form-data` for the proof file.
- Swagger UI, DEBUG only: `/api/v1/docs/`. The raw schema is at `/api/v1/schema/`
  (`?format=json` for JSON).

## Changes in 1.1

For the Flutter developer. Every change adds a field or fixes the documentation, so a
1.0 client keeps working; the last column is what to adopt.

| # | Where | 1.0 | 1.1 |
| --- | --- | --- | --- |
| 1 | `GET /me/` → `company` | `{country, time_zone, currency}` | adds `payments: {accepted_proofs: [ProofType], file_required_for: [ProofType]}`. Offer only `accepted_proofs` on the payout screen; ask for a file for the types in `file_required_for` (today `["photo"]`). See [Payments](#payments). |
| 2 | Every `AttendanceRecord`: day detail, records `PUT`, submit, correction `record`, sync `records` and `corrections[].record` | `{id, client_uuid, day_id, worker_id, …}` | adds `site_id` (int) and `date` (`YYYY-MM-DD`) of the record's day, so a synced record can be stored without its day. |
| 3 | `POST /auth/pin/` errors | one generic example per status | `401` examples: wrong PIN `{"attempts_left": 2}`; unknown phone / no PIN / wrong role `{}` (the same answer for all three). `423` example: `{"locked_until": "2026-10-31T06:57:00Z"}`. See [PIN errors](#authentication-pin--device-token). |
| 4 | Cash sheet `lines[].pay_line_id` | "Send this to `POST /payments/`" | a documented guarantee: stable for a (pay period, worker) while the figures are recomputed. See [Pay line ids](#pay-line-ids). |
| 5 | Every example in `openapi.yaml` | unrelated ids and names per endpoint (a 42-worker count next to 4 records) | one data set: site 3, foreman 7, manager 4, workers 63/58/51/44 (Agbo, Atsu, Dosseh, Mensah), day 210 on 2026-10-31, times in order (marked 07:41 → submitted 07:42 → corrected 09:15 → approved 16:10 → synced 16:12 → paid 16:31). Source: `polaris_workforce/apps/api/examples.py`. |
| 6 | `POST /auth/logout/` | "unsent writes cannot be sent after logout" | **that was wrong**: writes queued before a logout are accepted after the same user signs in again (new token). See [Logout and the outbox](#logout-and-the-outbox). |
| 7 | `POST /sites/{id}/days/{date}/submit/` request | `{client_uuid, foreman_note}` | adds optional `submitted_at` (ISO datetime, phone clock when Submit was tapped). The office shows it and flags a roll call that reached the server 1 h or more later. |
| 8 | `400 validation_error` details for list fields | `{"records": {"0": {…}}}` (an object keyed by index) for some errors | always a JSON list: `{"records": [{}, {"worker_id": ["…"]}]}`. See [Errors](#errors). |
| 9 | Unknown URL under `/api/` | Django's HTML 404 page | JSON: `404 {"code": "not_found", "message": "Not found.", "details": {}}`. |
| 10 | Documentation fixes | — | records `PUT` lists its `409 conflict` (a `client_uuid` of another record); `POST /payments/` says that `amount` must equal what is left to pay on the line, gives the real proof file limit (jpg/jpeg/png/webp/pdf, ≤ 5 MB, not 2 MB) and `400`/`409` examples; submit has `409` examples (`unmarked_worker_ids`, already submitted). The offline section no longer mentions drift (the app uses sqflite). |

### Security changes after 1.1 (Wave 3, H-sec)

The JSON shapes do not change; the contract version stays `1.1.0`.

| # | Where | Before | Now |
| --- | --- | --- | --- |
| S1 | `photo_url`, `proof_url` | public `/media/…` URLs (guessable file names) | `/api/v1/files/workers/{id}/photo/` and `/api/v1/files/proofs/{id}/`: **send the `Authorization` header** when loading them; `404` outside your sites. See [Photos and proofs](#photos-and-proofs). |
| S2 | Device token | never expired | `401 not_authenticated` after 30 days without use; sign in with the PIN again (keep the outbox). |
| S3 | Proof files | checked by extension | the first bytes must match the extension (`400` on `proof_file`). |
| S4 | Documentation | "submitted → 423" for records `PUT`; item-error lists "stop at the last error" | a records batch that changes nothing is `200` whatever the day's status; the records `PUT` item-error list is full length. See [Idempotency](#idempotency-client_uuid) and [Errors](#errors). |

## Endpoints

| Method & path | Purpose | Success | Wave 2 owner |
| --- | --- | --- | --- |
| `GET /health/` | Is the server reachable? No auth. | 200 | done |
| `POST /auth/pin/` | Phone + PIN + device ID → device token. No auth; throttled. | 200 | A |
| `POST /auth/logout/` | Revoke this device's token | 204 | A |
| `GET /me/` | Profile, company (time zone, currency, payment proof rules), assigned sites | 200 | A |
| `GET /sites/{site_id}/crew/?date=` | Workers expected on the site that day | 200 | B |
| `GET /sites/{site_id}/days/?from=&to=` | Day summaries (home screen "This week") | 200 | C |
| `GET /sites/{site_id}/days/{date}/` | The day and its records (`not_started` if none) | 200 | C |
| `PUT /sites/{site_id}/days/{date}/records/` | Batch upsert of records | 200 | C |
| `POST /sites/{site_id}/days/{date}/submit/` | Submit the roll call | 200 | C |
| `POST /records/{record_id}/corrections/` | Correct a record after submission (reason required) | 201 / 200 replay | C |
| `GET /sync/?since=` | Days, records and corrections changed since | 200 | C |
| `GET /sites/{site_id}/payouts/{date}/` | Cash sheet for the day | 200 | D |
| `POST /payments/` | Record a payment with proof | 201 / 200 replay | D |
| `GET /files/workers/{worker_id}/photo/` | A worker's photo (`photo_url`) | 200 / 304 | H-sec |
| `GET /files/proofs/{payment_id}/` | A payment's proof file (`proof_url`) | 200 / 304 | H-sec |

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

   **Several companies:** one server can host several companies, each with separate data.
   The phone needs no company code: a phone number belongs to one user across the whole
   server (the Users screen refuses a number another user already has, in any company),
   so the number finds the user and the user's company. Everything the token then reads
   or writes (`/me/` company settings, sites, crews, days, payouts) is that company's.
   Users of a company that was made inactive get `401` (sign-in and token).
4. Every other call sends:

   ```
   Authorization: Device <token>
   ```

5. `401 not_authenticated` means the token is missing, wrong, revoked (logout, or an
   admin revoked the device on the Users screen) or **expired**: a token unused for 30
   days (the server's `DEVICE_TOKEN_IDLE_DAYS`) stops working. Clear the token and show
   the PIN screen. **Keep the outbox**: send it after the foreman signs in again.
6. Worker photos (`photo_url`) and payment proofs (`proof_url`) are protected downloads
   under `/api/v1/files/`: **send the same `Authorization: Device <token>` header** when
   you load them (an image widget without the header gets `401`). See
   [Photos and proofs](#photos-and-proofs).

PIN errors:

- Wrong PIN for a foreman (or admin) who has a PIN: `401 not_authenticated`, with
  `details.attempts_left` (tries left before the lock). The 5th wrong PIN answers
  `attempts_left: 0` plus `details.locked_until`.

  ```json
  {"code": "not_authenticated", "message": "Wrong PIN.", "details": {"attempts_left": 2}}
  ```

- An unknown phone number, a user without a PIN, a disabled user, a user of an inactive
  company or without a company (platform admin), or a role that cannot
  sign in on the phone (site manager, accountant): the **same plain** `401
  not_authenticated`, `"message": "Wrong phone number or PIN."`, `"details": {}`. The
  answer does not say which of these it was, so show one message ("Wrong phone number
  or PIN").
- Locked after 5 wrong PINs (for 15 minutes; even the right PIN is refused):
  `423 locked`, with `details.locked_until` (ISO datetime). Show the time and keep the
  keypad disabled until then.

  ```json
  {"code": "locked", "message": "Too many wrong PINs. Try again later.", "details": {"locked_until": "2026-10-31T06:57:00Z"}}
  ```

- More than 10 tries a minute from one IP address: `429 throttled`, with
  `details.retry_after` (seconds; also sent in the `Retry-After` header).

The server stores only `sha256(token)` (`ForemanDevice.token_hash`) and updates
`last_seen_at` at most once a minute. Office staff can also call the API with their
browser session (session authentication; unsafe methods need CSRF), which is useful in
Swagger UI.

### Logout and the outbox

`POST /auth/logout/` revokes the token used for the call, nothing else. Records,
submissions, corrections and payments belong to the **user and the site**, never to the
token, so:

- Writes queued in the outbox before a logout are accepted after **the same user** signs
  in again on the device (with a new token). Send them as they are, with their original
  `client_uuid`s.
- A write that reached the server before the logout (its response lost) replays with
  the new token: `200` and the original object (`Idempotent-Replayed: true` for payments
  and corrections).
- The same holds after a `401` because an admin revoked the device: sign in, then send
  the outbox.
- Don't send one foreman's outbox under another user's token: a payment replayed by
  someone else is `409 conflict` (the first foreman paid it), and records or a
  submission would be saved under the other user's name. When a different user signs in
  on the phone, keep the previous user's outbox aside until they sign in again.

Drain the outbox before logging out when you can; it is not required.
`tests/test_api_outbox.py` checks this end to end (PIN sign-in, logout, sign-in again).

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
| 400 | `validation_error` | Bad body or query string | field → list of messages; a JSON list for list fields (below); `non_field_errors` for the rest |
| 401 | `not_authenticated` | No token, a wrong, revoked or expired (30 days unused) token, a wrong PIN | wrong PIN: `attempts_left` |
| 403 | `permission_denied` | Signed in but not allowed, e.g. a site you are not assigned to | — |
| 404 | `not_found` | No such object, or no such URL under `/api/` | — |
| 405 | `method_not_allowed` | Wrong HTTP method | — |
| 409 | `conflict` | The request clashes with the current state (a `client_uuid` reused for another request, submitting with unmarked workers, paying a sheet that is not open) | context, e.g. `unmarked_worker_ids`, `client_uuid` |
| 415 | `unsupported_media_type` | Wrong `Content-Type` | — |
| 423 | `locked` | A write that would change a submitted, approved or locked day or a locked pay period, or the device is locked after failed PINs | e.g. `status`, `locked_until` |
| 429 | `throttled` | Too many requests | `retry_after` (seconds) |
| 500 | `server_error` | A bug on the server | — |
| 501 | `not_implemented` | Reserved for an endpoint in the contract that is not built yet (none today) | — |

**Errors on list items** (the records of a batch `PUT`) are always a JSON list, never an
object keyed by index, in the order sent; an item without errors is `{}`. The records
`PUT` sends the **full-length list**: one entry per item sent, including `{}` entries
after the last error. Other list errors (serializer checks) may stop at the last item with
an error. Accept both: an item with no entry, or with `{}`, had no errors.

```json
{"code": "validation_error", "message": "Invalid input.",
 "details": {"records": [{}, {"worker_id": ["This worker is not on the crew of this site that day."]}, {}]}}
```

Too many items is an error on the field itself, not on an item:
`{"records": {"non_field_errors": ["Ensure this field has no more than 200 elements."]}}`.

**Unknown URLs** under `/api/` (a typo, an impossible date such as `2026-02-30` in the
path, a site id that is not a number) answer in the same format:
`404 {"code": "not_found", "message": "Not found.", "details": {}}`. Every path ends with
a slash; without it, the server redirects (`301`) to the path with the slash. Office pages
outside `/api/` keep the HTML 404 page.

## Idempotency: `client_uuid`

The app saves every write to the phone first, with a **`client_uuid`** (UUID v4) made
when the foreman acts, then sends it from an outbox. It may send the same write several
times (timeouts, a lost response, a crash). The server applies it only once:

| Kind | Endpoints | First time | Repeat with the same `client_uuid` |
| --- | --- | --- | --- |
| Create | `POST /payments/`, `POST /records/{id}/corrections/` | `201` + the new object | `200` + the **original** object, header `Idempotent-Replayed: true` |
| Upsert | `PUT /sites/{id}/days/{date}/records/` | `200` + the day | `200` + the day while the server still has those values (whatever the day's status); `423` if they changed since (see below) |
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
- **Drop a write from the outbox as soon as the server acknowledges it** (`200`/`201`).
  Resend only writes without an answer (timeout, lost connection, `5xx`). A replay is a
  safe no-op only while nothing changed on the server since: once a record was
  corrected (in the office or by a correction from the phone) or its day submitted,
  resending the **old** records batch answers `423 locked`, because it would undo the
  correction. The server keeps the right state; the app must not show that `423` as a
  failure of a write that already succeeded — which it avoids by never replaying an
  acknowledged write.
- A records batch that changes nothing (the server already has every value in it)
  answers `200` with the day whatever the day's status, even `submitted` or `locked`.
  Only a batch with a change on a `submitted`, `approved` or `locked` day is `423`.

The server side of this is in `polaris_workforce/apps/api/idempotency.py`
(`run_once`, `apply_changes`, `IdempotentCreateMixin`).

## Sync: `since` and `server_time`

```
GET /api/v1/sync/?since=2026-10-31T07:45:00Z
→ {"server_time": "2026-10-31T16:12:30Z", "since": "…", "days": […], "records": […], "corrections": […]}
```

- Every record carries `day_id`, `site_id` and `date`, so it can be stored even when its
  day is not in the same response (a correction changes the record, not always the day).

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

## Payments

- **Proof rules** come from `GET /me/` → `company.payments`:
  - `accepted_proofs`: the proof types `POST /payments/` accepts (a company setting).
    Offer only these. `none` (no proof) is accepted only when it is listed. Anything
    else: `400` on `proof_type`.
  - `file_required_for`: the types that need a `proof_file` (today `["photo"]`). For a
    signature or thumb mark the file is optional (it may be on the paper sheet). Missing
    file: `400` on `proof_file`.
  - The file: jpg, jpeg, png, webp or pdf, at most 5 MB, and its first bytes must match
    the extension (a PNG named `.jpg` is refused: `400` on `proof_file`). Send
    `multipart/form-data` when there is one, JSON otherwise.
- `amount` must equal what is left to pay on the line (its `net` when unpaid),
  otherwise `400` on `amount`.

### Corrections after payment: `net` can include an adjustment

If a day the worker was already paid for is corrected later (for example marked absent),
the difference is settled on the worker's **next** pay at that site: deducted when they
were overpaid, added when they were underpaid. In `GET payouts/` that adjustment is only
in `net`; `gross` stays what was earned that pay and `advance_deduction` the advance
repayment. So on those lines `net ≠ gross − advance_deduction`. Always hand over `net`
(and send it as `amount`). `net` is never negative; whatever doesn't fit carries to the
following pay. The office cash sheet and the payslip show the adjustment as its own line.

### Pay line ids

`pay_line_id` is the pay of one worker for one pay period (one line per period and
worker; with daily pay, a period is one day). The server recomputes the unpaid lines
whenever the attendance changes (approval, corrections). The guarantee:

- **Recomputing keeps the id.** The line is updated in place: `units`, `gross`,
  `advance_deduction` and `net` may change, the id does not.
- **A paid line is frozen**: never recomputed, never deleted. Nothing changes in a
  locked period.
- **The id changes only if the worker leaves the sheet and comes back.** When a worker
  has no worked day left in the period (for example their only day is corrected to
  absent), their unpaid line is deleted; if a later correction puts them back, a new
  line with a new id is made. (The same happens if the office changes the pay frequency,
  which moves the days to another period.)

So: pay from the latest `GET /sites/{id}/payouts/{date}/`. A payment queued for a line
that no longer exists gets `404 not_found`, and one whose `net` changed gets `400` on
`amount`: refresh the sheet and show it to the foreman again.

## Photos and proofs

Uploaded files are never public. `photo_url` (crew list, cash sheet) and `proof_url`
(`POST /payments/`) are absolute URLs of protected downloads:

| URL | What |
| --- | --- |
| `GET /api/v1/files/workers/{worker_id}/photo/` | A worker's photo |
| `GET /api/v1/files/proofs/{payment_id}/` | A payment's proof file |

- **Photos and proofs need the `Authorization: Device <token>` header**, like every
  other call. With Flutter's `Image.network` / `CachedNetworkImage`, pass it in
  `headers`.
- Only for workers and payments of your sites. A worker or payment of another site, one
  without a file, or a file that is gone: `404 not_found` (JSON). No token or an expired
  one: `401`.
- The answer is the file itself (`image/jpeg`, `image/png`, `image/webp`; proofs may be
  `application/pdf`, sent as an attachment), with an `ETag`. Cache the file and send
  `If-None-Match: <etag>` next time: `304 Not Modified` costs no download. A new photo
  of the worker gives a new `ETag` at the same URL.
- Don't build these URLs yourself; use the ones in the responses.

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

- Read from the local database (sqflite) and write to it first. The network only moves
  the outbox and refreshes the cache.
- Before draining the outbox, call `GET /health/` (it is cheap). Then send writes **in
  order, one at a time**: records → submit → corrections → payments. A correction needs
  the server's `record_id`, so send it only after the records `PUT` has returned.
- Retry network errors and `5xx` with backoff. Don't retry `400`, `403`, `404`, `409` or
  `423`: show them to the foreman. On `401`, sign in again and keep the outbox. On `429`,
  wait `retry_after`.
- Batch the roll call: one `PUT .../records/` holds up to 200 records.
- The network can be as slow as ~15 KB/s. Cache the crew list and worker photos (fetched
  with the `Authorization` header and revalidated with `If-None-Match`; they
  rarely change). Compress proof photos on the phone (JPEG, longest side ≤ 1280 px; the
  server takes up to 5 MB, but aim for a few hundred KB). Send `paid_at` from the moment
  of payment, and `submitted_at` from the moment the foreman tapped Submit, because an
  offline write can reach the server hours later.
- `GET /sites/{id}/days/{date}/` always returns a day, with `status: not_started` and
  `id: null` when nothing has been saved yet.
- Every endpoint is implemented; `501 not_implemented` stays in the error codes for
  future endpoints. The examples in `openapi.yaml` are one coherent data set
  (`polaris_workforce/apps/api/examples.py`) that the mock data can follow.

## Regenerating the contract

The schema is generated from the serializers and `@extend_schema` declarations in
`apps/<app>/api/`. After changing any of them, regenerate the file and commit it with
the code:

```sh
cd polaris_workforce
DJANGO_DEBUG=true uv run python manage.py spectacular --file ../docs/api/openapi.yaml --validate --fail-on-warn
```

`tests/test_api_schema.py` regenerates the schema and fails if `openapi.yaml` is out of
date.

Where to change things:

- Endpoints and serializers: `apps/<app>/api/` (the Wave 2 owner of that app).
- Example values: `apps/api/examples.py`, one data set for every endpoint
  (`tests/test_api_examples.py` checks that they validate and agree).
- Shared enums, the error format, money and time fields, authentication, idempotency:
  `apps/api/`. These are contract changes, so coordinate with the lead.
- Routes: each app's `api/urls.py`. `apps/api/urls.py` includes them all, once each.
