#!/bin/sh
# Entry point of the Polaris Workforce image.
#
#   web         migrate, collect static files, then serve with gunicorn (default)
#   scheduler   run send_messages / send_notifications on their schedule
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
  scheduler)
    exec /app/docker/scheduler.sh
    ;;
  manage)
    shift
    exec python manage.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
