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
uv run python manage.py createsuperuser  # first admin account
uv run python manage.py runserver        # http://127.0.0.1:8000
```

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
