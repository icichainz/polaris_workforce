"""UI shell: office URL map, sidebar active state, component gallery, sign-in page."""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

# The office URL map from docs/implementation-plan.md (section 3, F2).
OFFICE_URLS = [
    ("dashboard:home", "/"),
    ("dashboard:reports", "/reports/"),
    ("core:settings", "/settings/"),
    ("attendance:approvals", "/approvals/"),
    ("payroll:cash_sheets", "/cash-sheets/"),
    ("payroll:periods", "/pay-periods/"),
    ("payroll:advances", "/advances/"),
    ("workers:list", "/workers/"),
    ("worksites:list", "/sites/"),
    ("accounts:users", "/users/"),
]


@pytest.fixture
def signed_in_client(client, db):
    # An admin: every office page is open to them (settings and reports are role-restricted).
    user = get_user_model().objects.create_user(
        username="edem",
        password="s3cret-pass",
        first_name="Edem",
        last_name="Kpodar",
        role="admin",  # office pages are role-checked (Wave 2)
    )
    client.force_login(user)
    return client


@pytest.mark.parametrize(("url_name", "path"), OFFICE_URLS)
def test_url_map_paths(url_name, path):
    assert reverse(url_name) == path


@pytest.mark.parametrize(("url_name", "path"), OFFICE_URLS)
def test_office_pages_require_login(client, url_name, path):
    response = client.get(reverse(url_name))

    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.parametrize(("url_name", "path"), OFFICE_URLS)
def test_office_pages_render_with_active_sidebar_entry(signed_in_client, url_name, path):
    response = signed_in_client.get(reverse(url_name))
    html = response.content.decode()

    assert response.status_code == 200
    assert f'href="{path}" aria-current="page"' in html
    assert html.count('aria-current="page"') == 1
    # Every sidebar entry links somewhere that resolves.
    for _, other_path in OFFICE_URLS:
        assert f'href="{other_path}"' in html


def test_sidebar_shows_user_and_sign_out(signed_in_client):
    html = signed_in_client.get(reverse("dashboard:home")).content.decode()

    assert "Edem Kpodar" in html
    assert f'action="{reverse("logout")}"' in html
    assert 'aria-label="Sign out"' in html


def test_base_layout_loads_assets_and_htmx_csrf_header(signed_in_client):
    html = signed_in_client.get(reverse("dashboard:home")).content.decode()

    assert '<html lang="en">' in html
    assert "css/app.css" in html
    assert "vendor/htmx-2.0.4.min.js" in html
    assert "vendor/alpine-3.14.8.min.js" in html
    assert 'hx-headers=\'{"X-CSRFToken": "' in html


@override_settings(DEBUG=True)
def test_component_gallery_in_debug(client):
    response = client.get(reverse("dashboard:components"))

    assert response.status_code == 200
    html = response.content.decode()
    for marker in ("Approve day", "Half day", "Cash to pay today", "XOF", "Waiting for you"):
        assert marker in html


@override_settings(DEBUG=False)
def test_component_gallery_hidden_without_debug(client):
    response = client.get(reverse("dashboard:components"))

    assert response.status_code == 404


def test_login_page_has_form_and_language_switcher(client):
    response = client.get(reverse("login"))
    html = response.content.decode()

    assert response.status_code == 200
    assert f'action="{reverse("set_language")}"' in html
    assert 'name="language" value="en"' in html
    assert 'name="language" value="fr"' in html
    assert 'name="username"' in html
    assert 'name="password"' in html
    assert 'type="password"' in html
    assert "4-digit PIN" in html


@pytest.mark.django_db
def test_login_error_shows_message(client):
    response = client.post(reverse("login"), {"username": "nobody", "password": "wrong"})

    assert response.status_code == 200
    assert 'role="alert"' in response.content.decode()


@pytest.mark.django_db
def test_login_page_in_french(client):
    client.post(reverse("set_language"), {"language": "fr", "next": reverse("login")})
    html = client.get(reverse("login")).content.decode()

    assert '<html lang="fr">' in html
    assert 'name="language" value="fr" lang="fr" aria-pressed="true"' in html
