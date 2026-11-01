"""Celery: background jobs (the precompute rebuilds) and the periodic ones (beat).

Configured from the ``CELERY_*`` Django settings (``config/settings.py``); tasks live in
each app's ``tasks.py``. Run a worker and the scheduler next to the web server::

    celery -A config worker --loglevel INFO --concurrency 2
    celery -A config beat --loglevel INFO
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("polaris")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
