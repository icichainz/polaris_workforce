"""Settings for the test suite (``DJANGO_SETTINGS_MODULE`` in ``pyproject.toml``).

The production settings with a test secret key, a per-process cache and no dependence on
the developer's shell: ``DJANGO_DEBUG`` is forced off so every run sees the same URLs and
security settings, whatever ``DJANGO_DEBUG`` a developer exported for ``runserver``.
"""

import os

os.environ["DJANGO_DEBUG"] = "false"
os.environ.setdefault("DJANGO_SECRET_KEY", "tests-only-" + "not-a-secret-" * 4)
# No tracker, whatever a developer exported: tests of the config push set TRACKER_URL.
os.environ["TRACKER_URL"] = os.environ["TRACKER_PUBLIC_URL"] = ""

from config.settings import *  # noqa: E402, F403

# Fast and isolated per test process; production uses the shared file-based cache.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Tests never run collectstatic, so there is no manifest of hashed names to look up.
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# The test client speaks plain HTTP; production redirects it to HTTPS.
SECURE_SSL_REDIRECT = False

# No broker: nothing is queued, whatever CELERY_BROKER_URL a developer exported. Tests of
# the queueing set one themselves and catch what would be sent.
CELERY_BROKER_URL = ""

# Presence tracking: a fixed secret, so that tokens can be checked byte for byte
# (tests/test_presence_tokens.py).
TRACKER_SHARED_SECRET = "tests-only-tracker-secret-0123456789abcdef"
