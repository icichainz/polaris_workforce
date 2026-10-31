# Polaris Workforce — multi-agent implementation plan

**Goal:** build the Django backend and office dashboard with several agents working in
parallel, each in its own git worktree. A separate agent builds the Flutter foreman app UI
in parallel, against mock data. When the Django API is merged, we connect the Flutter app
to it.

**Sources of truth**

- Screens and design tokens: the design canvas (`design/project/`,
  https://claude.ai/artifact/Lm3QNB2xiEZn6bBj2FVD8c)
- Django scaffold: `polaris_workforce/` (Django 6.1, SQLite in WAL mode, custom `User` model)
- API contract: `docs/api/openapi.yaml` (written in Wave 1, frozen before the Flutter
  integration starts)

---

## 1. How the work is split

```
 Wave 0  Lead (sequential)       first commit · conventions · decisions
            │
 Wave 1  Foundation (3 parallel worktrees)
            ├── F1 Domain models (all apps)   ← the shared data contract
            ├── F2 UI shell (Tailwind, layout, components)
            └── F3 API contract (OpenAPI + DRF skeleton, URLs per app)
            │                                  │
            │                                  └──► Flutter track starts (FL1, FL2)
 Wave 2  Features (5 parallel worktrees, after Wave 1 is merged)
            ├── A Accounts, roles, PIN devices
            ├── B Sites & workers
            ├── C Attendance & approvals
            ├── D Payroll, cash sheets, advances
            └── E Settings, dashboard, reports
            │
 Wave 3  Integration & hardening (sequential + 1 Flutter agent)
            ├── I1 End-to-end flow tests, security pass
            ├── I2 Flutter ↔ Django API integration
            └── I3 Deployment (deferred)
```

**Why this split.** Parallel agents conflict in two places: **migrations** and **shared
files** (URLs, sidebar, settings). Wave 1 removes both risks before the parallel work
starts:

- **F1 writes every model up front.** After that, Wave 2 agents only add *fields or
  models inside their own app*, so migration conflicts become rare and local to one app.
- **F2 and F3 add every URL route, sidebar entry and API router** as placeholders, so
  Wave 2 agents never edit `config/urls.py`, the sidebar, or `INSTALLED_APPS`.

---

## 2. Wave 0 — Lead (before any agent starts)

| # | Task | Status |
| --- | --- | --- |
| 0.1 | **First commit** on `main` (scaffold, design, this plan). Worktrees can only branch from a commit. Not pushed. | Done |
| 0.2 | Repo layout: this repo holds `polaris_workforce/` (Django) and `polaris_foreman/` (Flutter). | Decided |
| 0.3 | API framework: **Django REST Framework**, with **drf-spectacular** for the OpenAPI schema. | Decided |
| 0.4 | `CLAUDE.md` with the conventions in §6; CI (`.github/workflows/django.yml`) running ruff, `makemigrations --check` and pytest. | Done |
| 0.5 | Shared files prepared so Wave 1 agents don't collide: `apps.core` app created; `rest_framework`, `drf_spectacular`, `apps.core` already in `INSTALLED_APPS`; DRF, drf-spectacular and factory-boy already in `pyproject.toml` / `uv.lock`. | Done |
| 0.6 | Start the Wave 1 agents, each in its own worktree (§7). | Next |

---

## 3. Wave 1 — Foundation (3 agents in parallel)

### F1 · Domain models — branch `feat/domain-models`

**Owns:** `apps/*/models.py`, `apps/*/migrations/`, `apps/*/admin.py`, `apps/core/`
(new: money helpers and settings resolution), `tests/factories.py`.

**Builds:**
- **`core`:** a `Money` helper (integers in the currency's smallest unit, plus the
  currency's decimal places), `TimeStampedModel`, `AuditLog` (who, what, when,
  before/after as JSON), `CompanySettings` (singleton: country, currency, decimal places,
  time zone, language, attendance mode, pay frequency, payment method, overtime rule,
  late/half-day rules, advance limit, working days), and
  `resolve_setting(name, site=None, worker=None)` for the company → site → worker
  inheritance.
- **`accounts`:** `ForemanDevice` (user, device ID, hashed token, hashed PIN, failed
  attempts, locked until, last seen, revoked at). The `User` model already exists.
- **`worksites`:** `Site` (status, address, manager, foremen, nullable override fields),
  `SiteAssignment` (worker, site, start date, end date).
- **`workers`:** `Trade`, `Worker` (number such as W-0142, names, phone, photo, ID
  document, active), `PayRate` (worker, type daily/hourly, amount, effective from).
- **`attendance`:** `AttendanceDay` (site, date, foreman, status Draft → Submitted →
  Approved / Rejected → Locked, submitted/approved timestamps), `AttendanceRecord` (day,
  worker, status P/A/H/L, arrival, departure, note, `client_uuid` unique),
  `AttendanceCorrection` (record, field, old value, new value, reason, user).
- **`payroll`:** `PayPeriod` (site, start, end, frequency, status), `PayLine` (period,
  worker, units, gross, deductions, net), `Advance` (worker, amount, deduction per pay,
  reason, balance), `AdvanceRepayment`, `CashIssue` (cash handed to a site),
  `Payment` (pay line, method, amount, paid by, paid at, proof type, proof file,
  `client_uuid` unique).
- Admin registration for every model, and factories for tests.
- Photos and proofs use `FileField` with extension and size validators, not
  `ImageField`: Pillow is not installed yet (slow network), and `ImageField` requires it.

**Done when:** `makemigrations --check` is clean; every model has a factory and a
create/read test; DB constraints exist (unique `client_uuid`, one record per worker per
day, an assignment's end date after its start); `resolve_setting` has unit tests for all
three levels.

### F2 · UI shell — branch `feat/ui-shell`

**Owns:** `templates/` (base, layouts, components, registration, and one placeholder
template per screen), `static/`, the Tailwind build files, `config/urls.py`, and the
office `urls.py` / `views.py` of every app (placeholder views only). Wave 2 agents then
replace the placeholders inside their own app, without touching `config/urls.py`.

**Office URL map** (namespace:name → path; owner in Wave 2):

| URL name | Path | Wave 2 owner |
| --- | --- | --- |
| `dashboard:home` | `/` | E |
| `dashboard:reports` | `/reports/` | E |
| `core:settings` | `/settings/` | E |
| `attendance:approvals` | `/approvals/` | C |
| `payroll:cash_sheets` | `/cash-sheets/` | D |
| `payroll:periods` | `/pay-periods/` | D |
| `payroll:advances` | `/advances/` | D |
| `workers:list` | `/workers/` | B |
| `worksites:list` | `/sites/` | B |
| `accounts:users` | `/users/` | A |
| `login` / `logout` | `/accounts/login/`, `/accounts/logout/` | F2 (styled) |

**Builds:**
- **Tailwind v4 build** using the standalone CLI (no Node needed at run time), with the
  tokens from the *Foundations* artboard as `@theme` values (ink, steel, line, ground,
  amber-500, blue-700, status colours). Fonts: Archivo and Inter.
- **htmx** and **Alpine.js** stored in `static/vendor/` (no CDN at run time).
- **App layout:** the sidebar from the *Sidebar* artboard, with every nav entry; the top
  bar; a page header block; messages.
- **Component partials** from the *Components* artboard: button (primary / secondary /
  dark / danger / ghost), status badge (attendance and workflow), stat tile, data table,
  form field (text, select, money field with currency suffix, "inherited setting" row),
  alert banners, workflow stepper, empty state.
- **Placeholder views** for every office screen (Dashboard, Approvals, Cash sheets, Pay
  periods, Advances, Workers, Sites, Reports, Users, Settings), so the sidebar links all
  resolve.
- A styled **sign-in** page (the *Sign in* artboard).

**Done when:** every sidebar link loads a placeholder page; a `/_components/` page in
DEBUG renders every component; layouts stack at phone width; contrast follows the
design (no grey text on white below 4.5:1).

### F3 · API contract — branch `feat/api-contract`

**Owns:** `apps/api/`, `apps/*/api/` (new packages), `docs/api/`, the `REST_FRAMEWORK`
and `SPECTACULAR_SETTINGS` blocks in `config/settings.py`.

**Builds:**
- **DRF configuration:** default authentication, permissions, renderers, the exception
  handler (error format below), pagination, throttling for `/auth/pin`.
- **An API package per app** (`apps/<app>/api/` with `serializers.py`, `views.py`,
  `urls.py`), each with stub views returning `501`. `apps/api/urls.py` includes every
  app's `api/urls.py` once, so Wave 2 agents only work inside their own app's `api/`
  package and never edit `apps/api/urls.py`.
- **`DeviceTokenAuthentication`** (`Authorization: Device <token>`), using the
  `ForemanDevice` model from F1. Until F1 is merged, F3 codes against the field names
  in F1's brief.
- **drf-spectacular** serves the schema at `/api/v1/schema/` and Swagger UI at
  `/api/v1/docs/` (DEBUG only).
- **`docs/api/openapi.yaml`:** the foreman app contract (below), with request and
  response examples. The Flutter agents build against this file.
- Error format (`{"code", "message", "details"}`), pagination, and the idempotency rule:
  every write from the phone carries a `client_uuid`; repeating the same write returns
  the original result.

**Draft foreman API (v1):**

| Method & path | Purpose | Agent |
| --- | --- | --- |
| `GET /health` | Server reachable | done |
| `POST /auth/pin` | Phone + PIN + device ID → device token | A |
| `POST /auth/logout` | Revoke this device's token | A |
| `GET /me` | Foreman profile and assigned sites | A |
| `GET /sites/{id}/crew?date=` | Workers expected on site that day (with photos) | B |
| `GET /sites/{id}/days/{date}` | The attendance day and its records | C |
| `PUT /sites/{id}/days/{date}/records` | Batch upsert of records (by `client_uuid`) | C |
| `POST /sites/{id}/days/{date}/submit` | Submit the roll call | C |
| `POST /records/{id}/corrections` | Correct a record after submission (reason required) | C |
| `GET /sites/{id}/payouts/{date}` | Cash sheet for the day (net per worker, cash issued) | D |
| `POST /payments` | Record a payment + proof (multipart, idempotent) | D |
| `GET /sync?since=` | Changes since the last sync (records, statuses, approvals) | C |

**Done when:** `docs/api/openapi.yaml` validates and matches what drf-spectacular
generates (a test compares them); `/api/v1/docs/` shows every endpoint as a stub
returning `501`; the contract covers all 5 phone screens.

**Merge order for Wave 1:** F1 → F3 → F2 (F3 refers to model names; F2 is independent).

---

## 4. Wave 2 — Features (5 agents in parallel, after Wave 1 is on `main`)

Each agent owns **one area**: its app's views, templates, services, forms, router and
tests. It may add fields or models **only inside its own app**. Changes needed in another
app are written down in the PR description for the lead to handle; agents don't edit
each other's apps.

### A · Accounts, roles and PIN devices — `feat/accounts`
- **Permissions:** role checks plus site scoping
  (`user.can(action, site)`, `scoped_sites(user)`), and a mixin/decorator applied to
  every office view.
- **Foreman devices** (the `ForemanDevice` model comes from F1): `POST /auth/pin`,
  `/auth/logout`, `/me`; PIN hashing; lockout after repeated failures; revoking a device
  from the Users screen.
- **Users & roles screen:** list, invite/edit, the permission table, site assignment for
  managers and foremen.
- **Done when:** a foreman's token cannot read another site (tested); a revoked device
  gets `401`; brute-force lockout is tested.

### B · Sites and workers — `feat/sites-workers`
- **Screens:** Sites list (status tabs, search), Site detail (week strip, crew,
  settings panel), Workers list (filters, pagination), Worker profile (month calendar,
  rate history, payments, advance), Add/edit worker (photo, pay type, inherited settings).
- **Services:** worker numbering (W-0001…), rate history (a new rate closes the
  previous one), site assignment rules.
- **API:** `GET /sites/{id}/crew`.
- Excel import of workers is a stretch goal.
- **Done when:** a rate change keeps the history; a crew list respects assignment dates;
  the screens match the artboards at desktop and phone widths.

### C · Attendance and approvals — `feat/attendance`
- **Service layer** (`apps/attendance/services.py`):
  `upsert_records` (idempotent), `submit_day`, `correct_record` (reason required,
  writes an `AttendanceCorrection` and an `AuditLog` entry), `approve_day`,
  `reject_day(note)`, and the status changes allowed between Draft, Submitted,
  Approved, Rejected and Locked.
- **Screens:** Approvals (the list of submitted days plus the detail panel with changed
  rows highlighted, approve / send back), and the attendance history tab on Site detail.
- **API:** days, records, submit, corrections, `GET /sync`.
- **Done when:** replaying the same upsert twice changes nothing; an approved day
  rejects edits without the correction path; every status change is tested.

### D · Payroll, cash sheets and advances — `feat/payroll`
- **Pay engine** (`apps/payroll/engine.py`, pure functions, no database access): units
  from attendance (P = 1, H = 0.5 by setting, L = 1 or minus late minutes by setting),
  daily or hourly rate in effect on that date, overtime, advance deduction capped at the
  net amount, rounding in the currency's smallest unit. **This is the most heavily
  tested module in the project.**
- **Pay periods:** generated per site from the frequency setting (daily, weekly, every 2
  weeks, monthly, every N days); a period is "ready" when all its days are approved;
  locking.
- **Cash sheet:** cash issued, total to pay, paid so far, left to pay, expected cash in
  hand, cash to return; marking a payment with proof (photo, signature, thumb mark);
  closing and locking.
- **Advances:** create (enforces the limit setting), repayment schedule, balance.
- **Payslip PDF** (the *Payslip* artboard, A4). **Agent D chooses the library** (for
  example WeasyPrint or ReportLab) and explains the choice in its report; consider that
  downloads are slow here and that WeasyPrint needs system libraries.
- **API:** `GET /sites/{id}/payouts/{date}`, `POST /payments`.
- **Done when:** the engine has table-driven tests for every rule and setting
  combination; payments are idempotent; a locked period rejects changes; cash
  reconciliation totals always add up (property-based test).

### E · Settings, dashboard and reports — `feat/settings-reports`
- **Settings screen:** every section of the *Settings* artboard, bound to
  `CompanySettings`; per-site override UI on Site detail (shared partial, coordinated
  with B through the lead).
- **Dashboard "Today":** stat tiles, the sites-today table, a "Needs attention" list
  (missing roll call, corrections after submission, absent 3 days in a row, unclosed
  cash sheets), a 7-day chart (Chart.js).
- **Reports:** labour cost by site, attendance rate by week, attendance by worker, CSV
  and Excel export (openpyxl).
- **Audit log viewer** (admin only).
- **Done when:** changing currency or decimal places changes how every amount is
  displayed; dashboard queries stay under 20 per page (`assertNumQueries`); exports open
  in Excel.

**Wave 2 dependencies:** none of the five blocks the others, because all models exist
from F1. D reads attendance records directly (tests use factories) and moves to C's
services once C is merged. **Merge order:** A → B → C → D → E. Every later branch
rebases onto `main` before its review.

---

## 5. Flutter track (starts after F3's first contract draft)

App folder: `polaris_foreman/`. It runs in its own worktree on `feat/flutter-ui`, so it
never touches the Django branches.

### FL1 · Foreman app UI — `feat/flutter-ui`
- **Stack:** Flutter (stable), Riverpod, go_router, drift (local SQLite on the phone),
  dio, flutter_secure_storage (device token), image_picker (proof photos), a signature
  pad, intl (EN/FR).
- **Theme** from the design tokens: ink, amber, status colours, Archivo/Inter, 56 px
  touch targets, status always shown as letter + word.
- **Screens** (the *Foreman app* page of the canvas): PIN sign-in, Today, Roll call,
  Review & submit, Cash payout.
- **Data layer:** repository interfaces, plus a **mock implementation** that serves
  data shaped exactly like `openapi.yaml`.
- **Offline first:** every write goes to drift first with a `client_uuid`, then into an
  outbox queue; a sync service shows the online/offline/pending state seen in the
  designs.
- **Done when:** all 5 screens work end to end on the mock data, in both airplane mode
  and online; widget tests for the roll-call row and the payout confirmation; golden
  tests for the theme.

### FL2 · API client (can start once FL1's data layer exists)
- Generate or hand-write the dio client from `openapi.yaml`; a real implementation of
  each repository behind a feature flag (`USE_MOCK_API`).
- Contract tests that check the mock responses against the OpenAPI schemas.

---

## 6. Rules for every agent

1. **Stay in your scope.** Only touch the files your brief says you own. Anything
   outside goes in the PR description under "Requests for other areas".
2. **Migrations only in your own app.** Run `makemigrations` for your app only, never
   edit another app's migrations, and never commit merge migrations. If `main` moved,
   rebase and regenerate your migration.
3. **Money:** integers in the currency's smallest unit, through `core.money`. No
   floats, anywhere.
4. **Business logic lives in services/engine modules**, not in views or templates.
   Views stay thin.
5. **Every write that changes pay or attendance writes an `AuditLog` entry.**
6. **Templates use the F2 component partials.** No one-off colours; Tailwind classes
   written out in full (no class names built from strings).
7. **All user-facing text is translatable** (`{% translate %}`, `gettext_lazy`).
8. **Before asking for review:** `uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run python manage.py makemigrations --check`
   all pass.
9. **SQLite:** never rely on database-specific features beyond SQLite 3.35 (no
   `select_for_update`); keep transactions short.

---

## 7. Worktrees and branches

```sh
# once, after the first commit on main
git worktree add ../polaris-wt/domain-models   -b feat/domain-models
git worktree add ../polaris-wt/ui-shell        -b feat/ui-shell
git worktree add ../polaris-wt/api-contract    -b feat/api-contract
git worktree add ../polaris-wt/flutter-ui      -b feat/flutter-ui

# Wave 2, after Wave 1 is merged into main
for area in accounts sites-workers attendance payroll settings-reports; do
  git worktree add ../polaris-wt/$area -b feat/$area main
done
```

- Each worktree runs `uv sync` and has its own `db.sqlite3` (git-ignored), so agents
  never share a database.
- Agents can also be started with the Agent tool's worktree isolation, which creates
  and removes the worktree automatically.
- The lead reviews each branch (code review plus running the checks), merges in the
  order given, and deletes the worktree afterwards (`git worktree remove`).

---

## 8. Wave 3 — Integration and hardening

| # | Task | Owner |
| --- | --- | --- |
| I1 | **Full-flow tests:** a foreman submits a roll call over the API → the manager approves in the dashboard → the cash sheet shows the right totals → payments over the API with proof → the period locks. Plus a **security pass**: site scoping on every view and endpoint, CSRF, file-upload limits, token revocation. | Lead + 1 agent |
| I2 | **Flutter integration:** switch `USE_MOCK_API` off, run the full flow on a device against a local server, handle sync conflicts (a record corrected in the office while the phone was offline: the server wins, the phone shows the change), retries, and expired tokens. | Flutter agent |
| I3 | **Deployment — deferred.** The owner will choose hosting after implementation. Keep the app deployable anywhere: settings from environment variables, `collectstatic`, one SQLite file whose path is configurable (`SQLITE_PATH`). | Later |
| I4 | **French translation** of every string (`makemessages` / `compilemessages`). | 1 agent |

**Release criteria:** a full day can be completed on a real phone (roll call → approval →
payout → lock) with the phone offline for part of it, and the numbers on the cash sheet,
pay period and payslip all agree.

---

## 9. Decisions

| Decision | Answer |
| --- | --- |
| First commit | Made on `main`, not pushed |
| Repo layout | This repo: `polaris_workforce/` + `polaris_foreman/` |
| API framework | Django REST Framework + drf-spectacular |
| Payslip PDF library | Agent D chooses and justifies |
| Production hosting | Deferred until implementation is finished |
