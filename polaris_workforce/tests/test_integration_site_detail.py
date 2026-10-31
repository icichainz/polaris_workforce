"""The site page links to check-in (geofence, badges) and shows the site's transfer
batches (Wave 4 integration), for the roles that may open those pages."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.checkin import services as checkin
from apps.payouts import services as payouts
from tests.factories import AdminUserFactory, CompanySettingsFactory, UserFactory
from tests.test_payouts_services import MOMO, World
from tests.test_payroll_services import MON

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    CompanySettingsFactory()
    return World()


def page(client, user, site):
    client.force_login(user)
    response = client.get(reverse("worksites:detail", args=[site.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_managers_get_the_geofence_and_badge_links(client, world):
    html = page(client, world.manager, world.site)

    assert 'id="site-checkin"' in html
    assert reverse("checkin:geofence", args=[world.site.pk]) in html
    assert f"{reverse('checkin:badges')}?site={world.site.pk}" in html
    assert "Set geofence" in html and "Not set" in html


def test_the_geofence_and_missing_badges_are_shown(client, world):
    admin = AdminUserFactory()
    checkin.set_geofence(
        world.site,
        latitude=Decimal("6.132100"),
        longitude=Decimal("1.223000"),
        radius_m=150,
        actor=admin,
    )
    checkin.issue_badge(world.yao, admin)

    html = page(client, admin, world.site)

    assert "150 m around the site" in html and "Edit geofence" in html
    assert "workers without a badge" in html


def test_the_sites_transfer_batches_are_listed(client, world):
    batch = payouts.create_batch(world.manager, MON, MOMO, world.site)

    html = page(client, world.manager, world.site)

    assert 'id="site-payouts"' in html
    assert reverse("payouts:batch", args=[batch.pk]) in html and batch.reference in html


def test_an_accountant_sees_batches_but_no_checkin_links(client, world):
    payouts.create_batch(world.manager, MON, MOMO, world.site)
    html = page(client, UserFactory(role="accountant"), world.site)

    assert 'id="site-checkin"' not in html
    assert reverse("checkin:geofence", args=[world.site.pk]) not in html
    assert 'id="site-payouts"' in html


def test_another_sites_manager_cannot_open_the_page(client, world):
    from tests.factories import SiteManagerFactory

    client.force_login(SiteManagerFactory())
    assert client.get(reverse("worksites:detail", args=[world.site.pk])).status_code == 403
