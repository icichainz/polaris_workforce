#!/bin/sh
# The two periodic jobs from the README's deployment notes, without cron:
#   send_messages       every 2 minutes  (flush the outbox: payslip texts, PINs, retries)
#   send_notifications  every 15 minutes (evaluate the alert rules, then send)
# A failing run is logged and retried on the next tick; the loop never stops.
set -u

echo "scheduler: started"
while true; do
  minute=$(date -u +%M)
  minute=${minute#0}
  if [ $((minute % 15)) -eq 0 ]; then
    python manage.py send_notifications || echo "scheduler: send_notifications failed"
  elif [ $((minute % 2)) -eq 0 ]; then
    python manage.py send_messages || echo "scheduler: send_messages failed"
  fi
  # Sleep to the start of the next minute.
  sleep $((60 - $(date -u +%S | sed 's/^0//')))
done
