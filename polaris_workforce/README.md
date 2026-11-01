# Polaris Workforce

Attendance and daily pay for construction crews: foremen take the roll call,
site managers approve it, and workers are paid with proof of receipt.

## Stack

- **Backend and office dashboard:** Django, server-rendered templates (Tailwind and light JS to come)
- **Database:** SQLite in WAL mode (see `DATABASES` in `config/settings.py`)
- **Foreman app:** Flutter, talking to the JSON API under `/api/v1/`

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync                                  # install dependencies into .venv
export DJANGO_DEBUG=true                 # local development (DEBUG is off by default)
uv run python manage.py migrate          # create db.sqlite3
uv run python manage.py createsuperuser  # platform admin (no company)
uv run python manage.py runserver        # http://127.0.0.1:8000
```

One installation serves several companies, each with its own data (`apps/core/tenancy.py`).
A superuser made with `createsuperuser` belongs to no company: a **platform admin**, who
adds companies on the Companies page (`/companies/`) and switches between them in the
sidebar. Every other user belongs to one company and only ever sees that company. For a
demo, `manage.py seed_demo` fills one company (and a platform admin, `platform`); add a
second one with `manage.py seed_demo --keep --company "Other Co"`.

Configuration comes from environment variables; see `.env.example`. **`DEBUG` is off
unless `DJANGO_DEBUG=true` is set**, and with `DEBUG` off the app refuses to start without
`DJANGO_SECRET_KEY`, so every `manage.py` command on your machine needs
`DJANGO_DEBUG=true` (export it, or `DJANGO_DEBUG=true uv run python manage.py …`). Tests
don't: pytest uses `tests/settings.py` (production settings with a test secret key and an
in-memory cache).

Uploaded worker photos and payment proofs are never served from `MEDIA_ROOT`, not even
in DEBUG: they get random file names and are downloaded through views that check the
viewer's sites (`apps/core/files.py`). In production, set `PROTECTED_MEDIA_MODE=nginx`
(or `apache`) so the web server sends the file after Django's check; see the production
checklist in `docs/security-review.md`.

## Deployment notes

To run the whole platform (Django, scheduler, nginx, tracker) with Docker Compose, see
[`docs/docker.md`](../docs/docker.md).

Static files are collected with hashed names (`app.3f2a9c81d0e4.css`, Django's
`ManifestStaticFilesStorage`): run `manage.py collectstatic` on every deploy, before the
app serves pages with `DEBUG` off, or pages fail to render. Whatever web server you put
in front, have it compress text responses (HTML, CSS, JS, JSON) and send hashed files
with `Cache-Control: public, max-age=31536000, immutable`; `docker/nginx/default.conf`
does both. Office pages are 5–10 times smaller compressed, which is most of the wait on
mobile data.

Besides the production checklist in `docs/security-review.md`, two commands must run
from cron (as the app user, with the production environment) once messaging is on:

```cron
*/15 * * * *  cd /srv/polaris/polaris_workforce && .venv/bin/python manage.py send_notifications
*/2 * * * *   cd /srv/polaris/polaris_workforce && .venv/bin/python manage.py send_messages
```

- `send_notifications` (every 15 minutes) checks each active company's notification
  rules (missing roll call, days waiting for approval, unclosed cash sheets, advances
  near their limit) and queues the texts; it also sends what is due.
- `send_messages` (every 2 minutes) only flushes the outbox: payslip texts, portal PINs
  and retries (with backoff, outside quiet hours).
- The default provider (`MESSAGING_PROVIDER=log`) sends nothing: it writes each text and
  phone number to the `polaris.messaging` log. Set a log retention, or a real provider,
  before go-live (see the security review).

## CSS (Tailwind v4)

The compiled stylesheet `static/css/app.css` is committed, so the app runs without
Tailwind. Rebuild it only after changing templates or `static/src/app.css` (the theme
tokens from the design foundations live there, in `@theme`).

The build uses the Tailwind v4 **standalone CLI** (no Node.js). Download
`tailwindcss-macos-arm64` (or the binary for your platform) from the
[Tailwind releases](https://github.com/tailwindlabs/tailwindcss/releases), make it
executable and put it at `~/.local/bin/tailwindcss`, on your `PATH`, or point
`TAILWINDCSS` at it. Never commit the binary.

```sh
scripts/build_css.sh           # minified build -> static/css/app.css
scripts/build_css.sh --watch   # rebuild on change while developing
```

htmx (2.0.4) and Alpine.js (3.14.8) are vendored in `static/vendor/`; Archivo and Inter
(latin variable subsets) are self-hosted in `static/fonts/`. Shared template partials
are in `templates/components/`; in DEBUG, `/_components/` shows every one of them.

## Checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
DJANGO_DEBUG=true uv run python manage.py makemigrations --check --dry-run
```

## Layout

| Path | Contents |
| --- | --- |
| `config/` | Settings, URLs, WSGI/ASGI |
| `apps/accounts/` | Users and roles (custom `User` model) |
| `apps/worksites/` | Sites and worker assignments |
| `apps/workers/` | Workers and pay rates |
| `apps/attendance/` | Attendance days, records, corrections |
| `apps/payroll/` | Pay periods, advances, payments, cash sheets |
| `apps/dashboard/` | Office dashboard pages |
| `apps/api/` | JSON API for the Flutter foreman app |
| `templates/` | Shared templates |

The sites app is named `worksites` so it never clashes with Django's own
`django.contrib.sites` (needed by some auth packages).
