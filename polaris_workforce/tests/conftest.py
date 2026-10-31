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
