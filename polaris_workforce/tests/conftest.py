import pytest
from django.utils import translation


@pytest.fixture(autouse=True)
def _english_by_default():
    """Start every test in English.

    Requests that switch language (e.g. set_language) leave the thread's active
    language behind, which would leak French messages into later tests.
    """
    translation.activate("en")
    yield
    translation.deactivate()


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    """Passwords and PINs use deliberately slow hashers; tests don't need that."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
