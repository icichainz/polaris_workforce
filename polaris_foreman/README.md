# Polaris Workforce — foreman app

Flutter app for foremen: the morning roll call (works offline), submit, and the
evening cash payout with proof of receipt. Contract: `../docs/api/openapi.yaml`
and `../docs/api/README.md`. Designs: `../design/project/Mobile*.dc.html`.

## Run

```sh
flutter pub get --offline          # every dependency is pinned to the local cache
flutter run                        # mock API (default, USE_MOCK_API=true)
flutter run --dart-define=USE_MOCK_API=false \
  --dart-define=API_BASE_URL=https://<server>/api/v1/   # real API
```

Real API (contract 1.1, `../docs/api/README.md`): `API_BASE_URL` defaults to
`http://10.0.2.2:8000/api/v1/` (the Android emulator's view of the host's
`runserver`); from the iOS simulator use `http://127.0.0.1:8000/api/v1/`.
Plain `http://` is allowed in Android debug builds and for local networking on
iOS only; real phones need `https://`. The device id (`device_id` of
`POST /auth/pin/`) is a UUID made once per install and kept in secure storage;
the device name is the platform ("Android phone"/"iPhone") until a model name
is available. A `401` on any call goes back to the PIN screen and keeps the
outbox: once the same foreman signs in again, the queued writes are sent. If
another foreman signs in while the outbox is not empty, it is held until its
owner is back.

Live smoke test against a running Django server (seed a foreman with a phone
and PIN, and workers with rates, first):

```sh
dart run tool/live_smoke.dart roll-call --base http://127.0.0.1:8765/api/v1/ \
  --phone +22890112233 --pin 4821
# approve the day in the office, then:
dart run tool/live_smoke.dart payout --base http://127.0.0.1:8765/api/v1/ \
  --phone +22890112233 --pin 4821
```

It drives the HTTP repositories through sign-in, me, crew, records (twice),
submit, sync, a correction, the payout and payments (multipart and JSON), and
checks every JSON answer against `openapi.yaml`.

Mock mode: phone `+22890112233`, PIN **1234** (other PINs: 401 with
`attempts_left`; 5 wrong PINs lock for 15 min: 423). A submitted roll call is
approved by the mock "site manager" after `MOCK_APPROVAL_SECONDS` (default 12),
which opens the cash payout. **Long-press the sync banner** on Today to simulate
losing the network (writes keep going to the phone and the outbox). The mock
server lives in memory: restart the app and its data is fresh again.

## Check

```sh
flutter analyze
flutter test
dart format --set-exit-if-changed lib test tool
flutter gen-l10n                   # after editing lib/l10n/*.arb
```

## Layout

- `lib/data/models/` — contract models (snake_case JSON), `Money` + `Currency`.
- `lib/data/repositories/` — one interface per area, `mock/` (in-memory server
  following the contract rules) and `http/` (`ApiClient`: base URL, headers,
  timeouts, error mapping; `HttpApi`: one method per endpoint).
- `lib/data/local/` — `LocalStore` (sqflite on the phone, memory in tests):
  crew cache, days, records, payout sheets and lines, payments, outbox.
- `lib/services/` — roll call, payout and `SyncService` (outbox drain + pull).
- `lib/state/` — Riverpod providers and the session.
- `lib/ui/` — screens (PIN, Today, Roll call, Review & submit, Cash payout).
- `lib/l10n/` — `app_en.arb`, `app_fr.arb` (generated code in `gen/`).

Tests: `contract_test` (the models round-trip every example of
`openapi.yaml`, and every example matches its schema), `http_client_test`
(each endpoint's request and response with `MockClient`, the error mapping),
`repository_suite_test` (one suite run against the mock repositories and the
HTTP client talking to the mock server over HTTP: same results, same errors,
answers valid against the schemas), `session_test`, `outbox_test`,
`widgets_test`.
