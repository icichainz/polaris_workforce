# Polaris Workforce — foreman app

Flutter app for foremen: the morning roll call (works offline), submit, and the
evening cash payout with proof of receipt. Contract: `../docs/api/openapi.yaml`
and `../docs/api/README.md`. Designs: `../design/project/Mobile*.dc.html`.

## Run

```sh
flutter pub get --offline          # every dependency is pinned to the local cache
flutter run                        # mock API (default, USE_MOCK_API=true)
flutter run --dart-define=USE_MOCK_API=false \
  --dart-define=API_BASE_URL=https://<server>/api/v1/   # real API (FL2)
```

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
dart format --set-exit-if-changed lib test
flutter gen-l10n                   # after editing lib/l10n/*.arb
```

## Layout

- `lib/data/models/` — contract models (snake_case JSON), `Money` + `Currency`.
- `lib/data/repositories/` — one interface per area, `mock/` (in-memory server
  following the contract rules) and `http/` (stub for FL2).
- `lib/data/local/` — `LocalStore` (sqflite on the phone, memory in tests):
  crew cache, days, records, payout sheets and lines, payments, outbox.
- `lib/services/` — roll call, payout and `SyncService` (outbox drain + pull).
- `lib/state/` — Riverpod providers and the session.
- `lib/ui/` — screens (PIN, Today, Roll call, Review & submit, Cash payout).
- `lib/l10n/` — `app_en.arb`, `app_fr.arb` (generated code in `gen/`).
