"""Office navigation without full page reloads (hx-boost, static/js/app.js).

The browser side (swapping #main, keeping the sidebar) is in app.js; these tests pin the
server side it relies on: boosted requests get full pages, the markup carries the hooks,
and links that return files or leave the office layout are not boosted.
"""

import pytest
from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse

from apps.core.htmx import wants_fragment
from tests.factories import AdminUserFactory, CompanySettingsFactory

pytestmark = pytest.mark.django_db

BOOSTED = {"HTTP_HX_REQUEST": "true", "HTTP_HX_BOOSTED": "true"}


@pytest.fixture
def admin(db):
    CompanySettingsFactory()
    return AdminUserFactory()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, False),
        ({"HTTP_HX_REQUEST": "true"}, True),
        (BOOSTED, False),
    ],
)
def test_wants_fragment_only_for_explicit_htmx(headers, expected):
    assert wants_fragment(RequestFactory().get("/", **headers)) is expected


def test_office_layout_carries_the_navigation_hooks(client, admin):
    client.force_login(admin)

    html = client.get(reverse("workers:list")).content.decode()

    assert '<div hx-boost="true"' in html
    assert '<main id="main" data-app-shell' in html
    assert '<nav id="sidebar-main-nav" hx-swap-oob="true"' in html
    assert 'id="nav-progress" hidden' in html
    assert "x-on:polaris-navigate.window" in html
    assert '"historyCacheSize": 0' in html
    # app.js registers Alpine components on alpine:init, so it loads before Alpine.
    assert html.index("js/app.js") < html.index("vendor/alpine")


def test_boosted_request_gets_the_full_page(client, admin):
    client.force_login(admin)

    html = client.get(reverse("workers:list"), **BOOSTED).content.decode()

    assert "<title>" in html
    assert 'id="sidebar-main-nav"' in html
    assert 'aria-current="page"' in html.split('id="sidebar-main-nav"', 1)[1]


def test_forms_that_leave_the_office_layout_are_not_boosted(client, admin):
    client.force_login(admin)

    html = client.get(reverse("workers:list")).content.decode()

    assert f'action="{reverse("logout")}" hx-boost="false"' in html


def test_download_buttons_are_not_boosted(client, admin):
    client.force_login(admin)

    reports = client.get(reverse("dashboard:reports")).content.decode()
    template = client.get(reverse("workers:import")).content.decode()

    assert f'href="{reverse("dashboard:reports_csv")}" hx-boost="false"' in reports
    assert f'href="{reverse("dashboard:reports_xlsx")}" hx-boost="false"' in reports
    assert f'href="{reverse("workers:import_template")}" hx-boost="false"' in template


def test_button_download_parameter():
    render = Template('{% include "components/button.html" with label="CSV" href="/x" %}')
    download = Template(
        '{% include "components/button.html" with label="CSV" href="/x" download=True %}'
    )

    assert "hx-boost" not in render.render(Context())
    assert '<a href="/x" hx-boost="false"' in download.render(Context())


def test_settings_page_has_no_inline_script(client, admin):
    client.force_login(admin)

    html = client.get(reverse("core:settings")).content.decode()
    main = html.split('id="main"', 1)[1]

    assert "<script" not in main
    assert 'x-data="settingsForm"' in main
