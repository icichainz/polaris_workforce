#!/bin/sh
# Entry point of the Polaris Workforce image.
#
#   web         migrate, collect static files, then serve with gunicorn (default)
#   worker      Celery worker: precompute rebuilds and the periodic jobs beat sends
#   beat        Celery beat: send_messages, send_notifications, refresh_precomputed on
#               their schedule (CELERY_BEAT_SCHEDULE in config/settings.py)
#   manage ...  run a manage.py command, e.g. `manage seed_demo` or `manage createsuperuser`
#   anything else is executed as is
set -eu

case "${1:-web}" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput --verbosity 0
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --threads "${GUNICORN_THREADS:-2}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --forwarded-allow-ips "*" \
      --access-logfile - \
      --error-logfile -
    ;;
  worker)
    # Few processes: every task writes to the one SQLite file.
    exec celery -A config worker \
      --loglevel INFO \
      --concurrency "${CELERY_CONCURRENCY:-2}"
    ;;
  beat)
    # The schedule's state is only "when did each job last run": /tmp is enough.
    exec celery -A config beat \
      --loglevel INFO \
      --schedule /tmp/celerybeat-schedule
    ;;
  manage)
    shift
    exec python manage.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
