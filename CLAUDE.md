# Polaris Workforce

Attendance and daily pay for construction crews. Foremen take the roll call on a phone,
site managers approve it in the office dashboard, workers are paid (usually in cash,
usually daily) with proof of receipt. Almost every rule is a setting.

- `polaris_workforce/` — Django 6.1 backend + office dashboard (server-rendered templates,
  Tailwind, htmx/Alpine), Django REST Framework API for the phone app, SQLite in WAL mode.
- `polaris_foreman/` — Flutter foreman app (offline-first).
- `design/project/` — design canvas files (screens and tokens). Treat as the UI spec.
- `docs/implementation-plan.md` — the multi-agent plan: waves, briefs, file ownership.
- `polaris_tracker/` and `docs/presence-tracking.md` — a separate presence-tracking track
  (Gleam service), planned after Wave 3. Not part of Waves 1–3; don't edit it.

## Commands (run inside `polaris_workforce/`)

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py runserver
```

Tracker (run inside `polaris_tracker/`, Gleam 1.12; see its README for the version pins):

```sh
gleam test
gleam format --check src test
TRACKER_DEBUG=1 gleam run
```

The network here is slow (~15 KB/s). Prefer `uv sync --offline` when the lock file has not
changed, and run any large download in the background.

## Rules

1. **Stay in your scope.** In a multi-agent wave, only edit the files your brief in
   `docs/implementation-plan.md` says you own. Anything else goes under
   "Requests for other areas" in your final report.
2. **Migrations only for your own app.** Never edit another app's migrations, never commit
   merge migrations. If `main` moved, rebase and regenerate.
3. **Money is never a float.** Integers in the currency's smallest unit, through
   `apps.core.money`. Currency and decimal places come from `CompanySettings`.
4. **Settings are inherited** company → site → worker. Read them through
   `apps.core.settings.resolve_setting`, never hard-code a default in a view.
5. **Business logic lives in `services.py` / `engine.py`**, not in views, serializers or
   templates.
6. **Anything that changes attendance or pay writes an `AuditLog` entry.** Locked
   attendance days and pay periods are read-only; changes go through corrections with a
   reason.
7. **Phone writes are idempotent:** every write from the API carries a `client_uuid`;
   repeating it returns the original result.
8. **Site scoping:** site managers and foremen only see their assigned sites — enforce it in
   every view, queryset and API endpoint, and test it.
9. **Templates use the shared component partials** in `templates/components/`. Write
   Tailwind classes out in full (never build class names from strings). No one-off colours.
10. **All user-facing text is translatable** (`{% translate %}`, `gettext_lazy`). English and
    French.
11. **SQLite:** no `select_for_update`, no database-specific SQL; keep transactions short.
12. **Before you finish:** pytest, ruff check, ruff format check and makemigrations check all
    pass. Commit on your feature branch; never push.
