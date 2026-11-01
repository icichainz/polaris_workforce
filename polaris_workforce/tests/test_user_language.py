import pytest

from apps.core.models import CompanySettings
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _html_lang(client, path="/"):
    html = client.get(path).content.decode()
    return html.split('<html lang="', 1)[1].split('"', 1)[0]


def test_signed_in_user_sees_their_own_language(client):
    client.force_login(UserFactory(role="admin", language="fr"))

    assert _html_lang(client) == "fr"


def test_blank_user_language_follows_the_company(client):
    company = CompanySettings.load()
    company.language = "fr"
    company.save()
    client.force_login(UserFactory(role="admin", language=""))

    assert _html_lang(client) == "fr"


def test_user_language_wins_over_the_company(client):
    company = CompanySettings.load()
    company.language = "fr"
    company.save()
    client.force_login(UserFactory(role="admin", language="en"))

    assert _html_lang(client) == "en"


def test_signed_out_pages_keep_the_switcher_and_browser_choice(client):
    company = CompanySettings.load()
    company.language = "fr"
    company.save()

    assert _html_lang(client, "/accounts/login/") == "en"
    assert _html_lang(client.__class__(HTTP_ACCEPT_LANGUAGE="fr"), "/accounts/login/") == "fr"


def test_api_keeps_accept_language(client):
    client.force_login(UserFactory(role="admin", language="fr"))

    response = client.get("/api/v1/no-such-endpoint/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.json()["message"] == "Not found."


def test_switcher_choice_wins_over_the_company(client):
    from django.conf import settings

    company = CompanySettings.load()
    company.language = "fr"
    company.save()
    client.force_login(UserFactory(role="admin", language=""))
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    assert _html_lang(client) == "en"


def test_browser_language_is_ignored_once_signed_in(client):
    client.force_login(UserFactory(role="admin", language=""))

    html = client.get("/", HTTP_ACCEPT_LANGUAGE="fr").content.decode()

    assert '<html lang="en" ' in html
