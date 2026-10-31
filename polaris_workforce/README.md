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
uv run python manage.py migrate          # create db.sqlite3
uv run python manage.py createsuperuser  # first admin account
uv run python manage.py runserver        # http://127.0.0.1:8000
```

Configuration comes from environment variables; see `.env.example`.

## Checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
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
