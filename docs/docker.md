# Running Polaris Workforce with Docker Compose

One command starts the whole platform on a machine with Docker:

| Service | What it runs | Reached at |
| --- | --- | --- |
| `nginx` | Entry point: static files, protected uploads, proxies the rest | `http://localhost:8080` |
| `web` | Django (office dashboard, worker portal `/portal/`, phone API `/api/v1/`) under gunicorn; runs migrations and `collectstatic` on start | through nginx |
| `scheduler` | `send_messages` every 2 min, `send_notifications` every 15 min | – |
| `tracker` | Gleam presence tracker (prototype) | `http://localhost:8080/tracker/` |

Data (SQLite in WAL mode, uploads, throttle cache) lives in the `data` volume; collected
static files in the `static` volume.

## Start

```sh
cp .env.example .env
# set DJANGO_SECRET_KEY and TRACKER_SHARED_SECRET in .env:
python3 -c "import secrets; print(secrets.token_urlsafe(60))"

docker compose up -d --build
docker compose ps                      # web should become "healthy"
```

Then open http://localhost:8080.

## First users or demo data

```sh
docker compose run --rm web manage seed_demo          # demo company, users, two weeks of data
docker compose run --rm web manage seed_demo --flush  # wipe and rebuild the demo
docker compose run --rm web manage createsuperuser    # a platform admin (no company)
```

The demo sign-ins are printed by `seed_demo` (office password `Polaris-demo-2026`).

## The phone app

The Flutter foreman app is not part of compose. Point it at this machine:

```sh
flutter run --dart-define=USE_MOCK_API=false --dart-define=API_BASE_URL=http://<LAN-IP>:8080/api/v1/
```

Add the LAN IP to `DJANGO_ALLOWED_HOSTS` (and `http://<LAN-IP>:8080` to
`DJANGO_CSRF_TRUSTED_ORIGINS`) in `.env`, then `docker compose up -d`. Release builds of
the app refuse plain HTTP: use HTTPS for real phones.

## Everyday commands

```sh
docker compose logs -f web scheduler        # app and job logs (the "log" messaging provider writes texts here)
docker compose run --rm web manage <command>
docker compose up -d --build                # after pulling new code (migrations run on start)
docker compose down                         # stop; data stays in the volumes
```

Backup: copy the SQLite file and the uploads out of the `data` volume while the app is
stopped, or use `sqlite3 /data/db.sqlite3 ".backup /data/backup.sqlite3"` inside `web`.

## Going to production

The defaults are for plain HTTP on one machine. Before real use, follow the production
checklist in `docs/security-review.md`, and in particular:

- Put HTTPS in front (or in this nginx) passing `X-Forwarded-Proto: https`; then set
  `DJANGO_SECURE_SSL_REDIRECT=true`, `DJANGO_SECURE_COOKIES=true`, and HSTS when ready.
- Set the real host names in `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS`.
- `DJANGO_NUM_PROXIES` is 1 (this nginx); add one per extra proxy in front.
- Messaging: choose a real provider; the `log` provider writes phone numbers, texts and
  portal PINs to the container logs — set a log retention.
- Back up the `data` volume.
