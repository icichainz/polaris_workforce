"""Settings for the test suite (``DJANGO_SETTINGS_MODULE`` in ``pyproject.toml``).

The production settings with a test secret key, a per-process cache and no dependence on
the developer's shell: ``DJANGO_DEBUG`` is forced off so every run sees the same URLs and
security settings, whatever ``DJANGO_DEBUG`` a developer exported for ``runserver``.
"""

import os

os.environ["DJANGO_DEBUG"] = "false"
os.environ.setdefault("DJANGO_SECRET_KEY", "tests-only-" + "not-a-secret-" * 4)

from config.settings import *  # noqa: E402, F403

# Fast and isolated per test process; production uses the shared file-based cache.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# The test client speaks plain HTTP; production redirects it to HTTPS.
SECURE_SSL_REDIRECT = False
