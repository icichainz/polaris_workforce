import pytest
from django.core.cache import cache
from django.utils import translation

from apps.core.tenancy import use_company

DEFAULT_COMPANY_SLUG = "polaris-construction"
# Requesting one of these gives the test a database (and so a default company).
_DB_FIXTURES = {
    "db",
    "transactional_db",
    "django_db_reset_sequences",
    "django_db_serialized_rollback",
}


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
def _empty_cache():
    """Start every test with an empty cache: throttles and lockouts would otherwise leak
    between tests, which reuse the same row ids."""
    cache.clear()


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    """Passwords and PINs use deliberately slow hashers; tests don't need that."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _current_company(request):
    """Every test with a database runs for a default company (``apps.core.tenancy``):
    factories, services and ``CompanySettings.load()`` use it. Requests set their own
    company from the signed-in user and put this one back afterwards. Use
    ``with use_company(other):`` to build a second company's data."""
    uses_db = request.node.get_closest_marker("django_db") is not None or bool(
        _DB_FIXTURES & set(request.fixturenames)
    )
    if not uses_db:
        yield None
        return
    request.getfixturevalue("_django_db_helper")
    from apps.core.models import Company

    company, _created = Company.objects.get_or_create(
        slug=DEFAULT_COMPANY_SLUG, defaults={"name": "Polaris Construction"}
    )
    with use_company(company):
        yield company


@pytest.fixture
def default_company(_current_company):
    """The company every test works for unless it switches (see ``_current_company``)."""
    return _current_company
