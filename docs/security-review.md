# Security review — Wave 3 (I1)

Branch `feat/e2e-security`, reviewed against `main` at `9a87295` (after K: API contract 1.1
and CL: cleanup). Scope: the Django backend in `polaris_workforce/` (office dashboard and
foreman API). The Flutter app and the presence tracker are out of scope.

Every check below is backed by a test unless it says "review only".

| Tests | What they cover |
| --- | --- |
| `tests/test_e2e_daily_pay.py` | The full day (XOF and EUR): settings → PIN → roll call → approval → payout → cash → payments → lock → payslip → dashboard/reports/CSV; audit trail; no secrets in audit rows or logs |
| `tests/test_e2e_corrections.py` | Correction after approval, refused payments until re-approved, pay follows the correction |
| `tests/test_e2e_advances.py` | Advance repaid exactly from the next pays; limit enforced in the office |
| `tests/test_e2e_weekly.py` | Site overriding pay frequency to weekly: one period, paid on the last working day |
| `tests/test_e2e_offline.py` | Late, reordered, duplicated outbox ends in the same state (and audit trail) as sending once |
| `tests/test_e2e_isolation.py` | Every URL name classified; site A manager/foreman against every site-B office page and API endpoint |
| `tests/test_security.py` | CSRF, roles, PIN lockout and throttle, token revocation, uploads, production settings, static checks, redirects, framing, escaping, CSV |

## Summary

No critical issue. Two bugs were fixed (PIN throttle bypass, CSV formula injection). The
main open items are about **production deployment** (uploaded proofs and photos need an
authenticated download path; `DEBUG` defaults to on) and two **money-integrity** questions
for the lead (a manager can lock pay periods although the permission table says they
cannot; a correction after a worker was paid leaves the paid line unchanged).

## What was checked

### Site scoping — pass

`tests/test_e2e_isolation.py::test_every_url_name_is_classified` walks the URL resolver and
fails when a URL name is not classified, so new screens must be added to the isolation
test. For every site-B object URL, site B's manager first opens it (200) — so the 403/404
for site A comes from scoping, not from a wrong URL. List pages are checked for site-B
names, with admin as the positive control.

| Office URL | Guard | Site A manager / foreman |
| --- | --- | --- |
| `dashboard:home`, `reports`, `reports_csv` | `scoped_sites`; reports: `role_required(admin, accountant, manager)` | no site-B data (`?site=<B>` ignored) / reports 403 for foreman |
| `attendance:approvals`, `decide` | `role_required(admin, manager)` + `_scoped_day` + `can_manage_site` | `?day=<B>` 404, decide 404 / 403 |
| `payroll:cash_sheets`, `periods` | `scoped_sites` (`?site=<B>` ignored) | no site-B data |
| `payroll:cash_sheet`, `cash_issue`, `cash_sheet_lock` | `get_site_or_403` + service role check | 403 |
| `payroll:cash_sheet_pay`, `payment_undo`, `payslip` | `get_site_or_403(line.period.site)` + service role check | 403 |
| `payroll:advances`, `advance_preview` | `scoped_workers`; `create_advance` checks `can_give_advance` | site-B worker refused, preview empty |
| `workers:list`, `create`, `detail`, `edit` | `scoped_workers`, `can_edit_worker` | 403 on site-B worker |
| `worksites:list`, `detail`, `crew`, `attendance`, `pay`, `settings`, `assign`, `end_assignment` | `scoped_sites` / `get_site_or_403` + `can_manage_site` for writes | 403 |
| `worksites:create`, `edit`; `core:settings`, `audit_log`; `accounts:*` | admin only | 403 |

| API endpoint | Guard | Site A foreman token / manager session |
| --- | --- | --- |
| `GET /me/`, `GET /sync/` | `scoped_sites(user)` | only site A |
| `GET /sites/{id}/crew/`, `/days/`, `/days/{date}/`, `/payouts/{date}/` | `get_site_or_403` | 403 |
| `PUT …/records/`, `POST …/submit/`, `POST /records/{id}/corrections/` | `IsForeman` + `get_site_or_403` (+ service `can_view_site`) | 403 |
| `POST /payments/` | `get_site_or_403(line.period.site)` + `can_pay` | 403 |
| `POST /auth/logout/` | revokes only the calling device | site B's device untouched |
| `/health/`, `/schema/`, `/auth/pin/`, JSON 404 | public | — |

### CSRF — pass

Every office URL is either in `GET_ONLY` or in the CSRF list in `tests/test_security.py`
(the test fails otherwise). Every state-changing office form — including login, logout,
language switch, password change/reset — answers 403 without the token, and an htmx
request with a forged `X-CSRFToken` header is refused. The htmx forms work with the header
that `base.html` sets. The API with a browser session needs CSRF (DRF
`SessionAuthentication`); device-token calls don't (no cookies). No `csrf_exempt` anywhere
(static check). The end-to-end tests use a CSRF-enforcing client throughout.

### Roles — pass, with one inconsistency (M1)

Tested through HTTP: an accountant cannot approve, issue cash, pay (office or API), undo
a payment or take a roll call, and can lock; a foreman in the office cannot approve, issue
cash, lock, undo, or open settings/users; a site manager cannot take the roll call over
the API; only managers and admins give advances (foreman and accountant are refused).

### PIN brute force, throttle, tokens — pass after fix (H1 fixed)

- 5 wrong PINs lock the user for 15 minutes (423 even with the right PIN, audit entry
  `accounts.pin_locked`); the 11th sign-in attempt per minute from one IP is 429 with
  `Retry-After`.
- **Fixed:** rotating `X-Forwarded-For` gave every request a fresh throttle bucket.
- Device token revoked on the Users screen, logout, or a disabled user → 401; signing in
  again issues a new token and the old one stays dead. Only `sha256(token)` is stored.

### Uploads — pass, production work needed (H2)

`.html`, `.svg`, `.exe` and > 5 MB proofs are refused (400); a `../../…/evil.jpg` file
name is stored under `MEDIA_ROOT/proofs/…` (Django strips the path). Every file field
(worker photo, payment proof, advance proof) has the extension and size validators.

### Audit trail and secrets — pass

The full-day test checks that each write appears exactly once in `AuditLog` despite
replays (settings, PIN set, day create, records upsert, submit, approve, cash issue,
payments, lock, day lock); corrections, advances and payment undo are checked in their
tests. No audit row and no log line contains the PIN, the device token, its hash or the
PIN hash.

### Production settings — pass, checklist below

With `DJANGO_DEBUG=false`: start-up fails without `DJANGO_SECRET_KEY`; secure + HttpOnly
session cookie, secure CSRF cookie, `ALLOWED_HOSTS` defaults to localhost only,
`X-Frame-Options: DENY`, `nosniff`, `SECURE_PROXY_SSL_HEADER`, no browsable API, no
Swagger UI, no `/media/` route. `manage.py check --deploy` warns only about
`SECURE_HSTS_SECONDS` and `SECURE_SSL_REDIRECT` (see checklist).

### Injection, redirects, framing, escaping — pass after fix (M2 fixed)

- No raw SQL (`.raw`, `.extra`, `RawSQL`, `cursor`) — static check. The ORM is used
  everywhere.
- Templates never use `|safe`, `mark_safe` or `autoescape off` (static check); values in
  Alpine/`onclick` attributes go through `escapejs`; a worker named `<script>` is escaped on
  the worker list, profile, crew and cash sheet.
- `next=` on login and on the language switch cannot leave the site
  (`https://evil…`, `//evil…`, `/\evil…`).
- Office pages, the payslip PDF and the API answer `X-Frame-Options: DENY`.
- **Fixed:** CSV export cells starting with `= + - @` (formula injection in Excel).

## Fixes made in this branch

| # | Fix | Where | Test |
| --- | --- | --- | --- |
| H1 | PIN throttle keyed on the client-controlled `X-Forwarded-For` header (DRF trusts the whole header when `NUM_PROXIES` is unset): an attacker could rotate it for unlimited PIN guesses across many phone numbers (still 5 per user per 15 min). Now `REST_FRAMEWORK["NUM_PROXIES"]` comes from `DJANGO_NUM_PROXIES` (default 0 = `REMOTE_ADDR`). | `polaris_workforce/config/settings.py` (REST_FRAMEWORK), `.env.example` | `test_pin_throttle_ignores_a_spoofed_forwarded_for_header` |
| M2 | CSV formula injection: a worker, trade or site name such as `=HYPERLINK(…)` ran as a formula in Excel. Text cells starting with `= + - @ \t \r` are now prefixed with `'`. | `polaris_workforce/apps/dashboard/reports.py` (`_text_cell`, `write_csv`) | `test_csv_export_neutralises_formulas` |

No API code (`apps/*/api/`, `apps/api/`) was changed and no test is marked `xfail`.

## Open issues

### High

**H2 — Uploaded proofs and worker photos have no protected download path.**
`config/urls.py:26` serves `MEDIA_ROOT` to anyone, without sign-in, when `DEBUG` is on.
With `DEBUG` off Django serves nothing, so the `proof_url`/`photo_url` links in the API and
the office break unless the web server serves `/media/` — and then they are public to
anyone with the URL. File names are guessable (`proofs/%Y/%m/<phone file name>`,
`apps/payroll/models.py:19`; `workers/<name>`, `apps/workers/models.py:42`): the first
`proof.jpg` of a month is stored as exactly that. These are photos of people receiving
cash, signatures and thumb prints.
*Fix:* random file names (`upload_to` callable with `uuid4().hex`), and a download view
that checks `can_view_site` for the payment's/worker's site (accepting session **and**
`Device` token auth, since the phone loads photos) and hands the file to the web server
with `X-Accel-Redirect`/`X-Sendfile`; the web server must not expose `MEDIA_ROOT`
directly. Keep the `static()` route only for local development.

### Medium

**M1 — Fixed** (owner decision: site managers lock their own sites; `LOCK_PERIODS` gives
them `OWN` and payroll's `can_lock` now calls `permissions.can(user, LOCK_PERIODS, site)`;
tests in `tests/test_payroll_adjustments.py` and `tests/test_accounts_permissions.py`).
*Was:* **A site manager can close and lock pay periods, but the permission table says they
cannot.** `apps/accounts/permissions.py:113` gives `LOCK_PERIODS` to admins and
accountants only (that is the matrix on the Users screen), while
`apps/payroll/services.py:125` (`can_lock`) also allows the site's manager, and the cash
sheet shows the lock button to managers. *Fix:* decide which one is right; then make
`can_lock` call `permissions.can(user, LOCK_PERIODS, site)` (or change the table), so the
screen and the rule cannot drift again.

**M3 — Fixed** (the paid line stays as paid; once the corrected day is approved, the
difference becomes a `PayAdjustment` settled on the worker's next pay at the site — shown
on Approvals, the cash sheet and the payslip, folded into `net` in the API; tests in
`tests/test_payroll_adjustments.py`, `tests/test_e2e_corrections.py`).
*Was:* **A correction after the worker was paid leaves the pay unchanged, silently.**
`apps/payroll/services.py:291` freezes a pay line once it has a payment. Probed: Ama is
paid 6000 for a present day, the foreman corrects her to absent (with a reason), the
manager re-approves; the line, payslip and cash sheet still say 1 day / 6000 and
"paid", while the attendance says absent. Nothing tells the manager. *Fix:* when a
correction changes a paid line's figures, flag it on the Approvals detail and the cash
sheet ("already paid 6000; corrected pay 0"), and record the difference as a deduction
or top-up on the worker's next pay line (an `other_deductions` adjustment with an audit
entry) instead of losing it.

**M4 — `DJANGO_DEBUG` defaults to on** (`config/settings.py:23`). A production server
started without the variable runs with debug pages (settings, SQL, source), the browsable
API, Swagger UI and public `/media/`. The `SECRET_KEY` guard does not help, because it is
only enforced when `DEBUG` is off. *Fix:* default to off and set `DJANGO_DEBUG=true` in the
local `.env`/shell (`.env.example` already does), or refuse to start with
`DEBUG` on when `DJANGO_ALLOWED_HOSTS` contains anything but localhost.

**M5 — Office sign-in has no rate limit or lockout.** `/accounts/login/` (Django's
`LoginView`) accepts unlimited password guesses; `/admin/login/` too. *Fix:* throttle by IP
and username (e.g. `django-axes`, or a small cache-based check in a custom `LoginView`),
and consider moving `/admin/`.

**M6 — Throttle state is per process.** No `CACHES` setting, so the PIN throttle uses
`LocMemCache`: each worker process has its own counter, and a restart resets it. Behind a
proxy, `DJANGO_NUM_PROXIES` must be set (see checklist) or every phone shares the proxy's
IP and the whole company gets 10 sign-ins a minute. *Fix:* a shared cache in production
(`django.core.cache.backends.db.DatabaseCache` works with SQLite, or `FileBasedCache`).

### Low

- **L1 — PIN lockout can be triggered by anyone who knows a foreman's phone number**
  (5 wrong PINs lock them out for 15 min, `apps/accounts/services.py:27`). Accepted
  trade-off; consider locking per (user, device) or showing the office a "locked" badge
  with an unlock button.
- **L2 — Registered phone numbers can be enumerated:** a wrong PIN for a real foreman
  answers `details.attempts_left`, an unknown number answers empty `details`
  (`apps/accounts/api/views.py:199`). Contract decision for K; the throttle limits it.
- **L3 — Device tokens never expire.** Revocation works (logout, Users screen, disable,
  role change). Consider expiring tokens unused for N days (`last_seen_at` is already
  stored).
- **L4 — Upload type is checked by extension only** (`apps/core/validators.py:10`): an HTML
  file named `x.jpg` is accepted (harmless while served with `nosniff` and an image type),
  and PDF is allowed for proofs. Check the magic bytes (JPEG/PNG/WebP/PDF) and, if H2 is
  done with `Content-Disposition: attachment`, the risk goes away. The API doc says proofs
  are ≤ 2 MB; the server accepts 5 MB (doc mismatch for K).
- **L5 — A stale outbox replay can get 423 for a write the server already applied.** The
  upsert replay is a no-op only while the record is unchanged: if an office/phone
  correction changed it since, resending the original batch answers 423 (the state stays
  right — the server wins). `apps/attendance/services.py:210–212`. The phone must drop a
  write once it was acknowledged (FL2/K: note it in `docs/api/README.md`).
- **L6 — A worker who moved between sites shows their last payments from every site on
  the profile**, to the manager of either site (`apps/workers/views.py:225–251`). The
  payslip link is already limited to visible sites, but the amounts and the proof file
  link (`proof_url`) are not. Apply the same `visible_sites` check to `proof_url` (and,
  if wanted, to the rows).
- **L7 — Fixed** (refresh on read is the system's: `actor=None`, actions
  `payroll.auto.*`, nothing written when nothing changed; `lock_sheet`/`lock_period` check
  the role first). *Was:* **Reads with side effects:** opening a cash sheet or
  `GET /payouts/` refreshes the pay periods and writes `payroll.period.*` audit rows with the viewer as actor
  (`apps/payroll/services.py:555`); `lock_sheet` refreshes before checking the role
  (`apps/payroll/services.py:891`). Not exploitable (same result for everyone), but the
  audit log shows read-only users as actors.

### Informational

- `/api/v1/schema/` is public (drf-spectacular default `SERVE_PERMISSIONS`). Fine for a
  documented contract; restrict with `SERVE_PERMISSIONS` if the schema should stay private.
- Password reset (`/accounts/password_reset/`) is routed but no e-mail backend is
  configured: set `EMAIL_*` or remove the route.
- `CSRF_COOKIE_HTTPONLY` can be turned on (htmx gets the token from the template, not the
  cookie).
- Sessions last two weeks (`SESSION_COOKIE_AGE` default). Shorter for shared office PCs.
- The E brief mentions an Excel export; only CSV exists (CSV is now formula-safe; an
  openpyxl export must escape the same way).

## Production checklist

Environment:

- [ ] `DJANGO_DEBUG=false` (and M4: make it the default).
- [ ] `DJANGO_SECRET_KEY`: 50+ random characters, kept out of the repository.
- [ ] `DJANGO_ALLOWED_HOSTS=<your domain>`; `DJANGO_CSRF_TRUSTED_ORIGINS=https://<your domain>`.
- [ ] `DJANGO_NUM_PROXIES=1` behind one reverse proxy that sets `X-Forwarded-For` (0 if
      Django faces the internet directly). Never more than the real number of proxies.
- [ ] `DJANGO_HSTS_SECONDS=31536000` once HTTPS works everywhere; add
      `SECURE_SSL_REDIRECT = True` (or redirect at the proxy) — the two
      `check --deploy` warnings.
- [ ] `SQLITE_PATH` and `MEDIA_ROOT` outside the web root, writable only by the app user;
      back both up together.
- [ ] A shared cache for the throttle (M6).

Web server / proxy:

- [ ] HTTPS only; the proxy sets `X-Forwarded-Proto` (used by `SECURE_PROXY_SSL_HEADER`)
      and **overwrites** (not appends to) any client `X-Forwarded-Proto`.
- [ ] Serve `collectstatic` output for `/static/`; **do not** expose `MEDIA_ROOT` (H2).
- [ ] Limit request bodies to ~6 MB (proofs are ≤ 5 MB).
- [ ] Rate-limit `/accounts/login/` and `/admin/` (M5) if not done in Django.

Before go-live:

- [ ] `python manage.py check --deploy` shows no warnings.
- [ ] H2 (protected media) and M1/M3 decided and done.
- [ ] Run the full test suite, including `tests/test_e2e_*.py` and `tests/test_security.py`.
