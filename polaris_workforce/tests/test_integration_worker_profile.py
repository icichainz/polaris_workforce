"""The worker profile links to the Wave 4 pages (integration): text messages, worker
portal access, payout account and QR badge — each for the roles that may open it, with
the worker's status, and never for a worker out of scope."""

import re
from urllib.parse import quote

import pytest
from django.urls import reverse

from apps.checkin import services as checkin
from apps.core.tenancy import use_company
from apps.messaging.models import MessagingSettings
from apps.portal import services as portal
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
    WorkerFactory,
)
from tests.test_payouts_services import momo_account

pytestmark = pytest.mark.django_db

KEYS = ("messages", "portal", "payout", "badge")


@pytest.fixture
def world():
    manager = SiteManagerFactory()
    foreman = ForemanFactory()
    site = SiteFactory(manager=manager, foremen=[foreman])
    worker = WorkerFactory(first_name="Yao", last_name="Mensah", phone="+228 90 12 34 56")
    SiteAssignmentFactory(site=site, worker=worker)
    return {
        "worker": worker,
        "admin": AdminUserFactory(),
        "manager": manager,
        "foreman": foreman,
        "accountant": UserFactory(role="accountant"),
        "other_manager": SiteManagerFactory(),
    }


def profile(client, user, worker):
    client.force_login(user)
    return client.get(reverse("workers:detail", args=[worker.pk]))


def shown(html) -> set[str]:
    return set(re.findall(r'id="connection-(\w+)"', html))


def block(html, key) -> str:
    match = re.search(
        rf'id="connection-{key}".*?</div>\s*(?:<div class="flex flex-wrap justify-end.*?</div>)?',
        html,
        re.S,
    )
    assert match, key
    return match.group(0)


@pytest.mark.parametrize(
    "role, expected",
    [
        ("admin", {"messages", "portal", "payout", "badge"}),
        ("manager", {"portal", "payout", "badge"}),
        ("accountant", {"payout"}),
    ],
)
def test_each_role_sees_the_links_it_may_open(client, world, role, expected):
    response = profile(client, world[role], world["worker"])

    assert response.status_code == 200
    assert shown(response.content.decode()) == expected


def test_the_admin_sees_every_status_and_link(client, world):
    worker = world["worker"]
    MessagingSettings.objects.update_or_create(company=worker.company, defaults={"enabled": True})
    portal.give_access(worker, actor=world["admin"], pin="4826")
    momo_account(worker, number="+228 90 11 22 33")
    checkin.issue_badge(worker, world["admin"])

    html = profile(client, world["admin"], worker).content.decode()

    assert reverse("messaging:worker", args=[worker.pk]) in html
    assert "Gets texts" in block(html, "messages")
    assert reverse("portal:office_worker", args=[worker.pk]) in html
    assert "Can sign in" in block(html, "portal")
    payout = block(html, "payout")
    assert "Mobile money · •••• 2233" in payout
    edit = reverse("payouts:account_edit", args=[worker.pk])
    assert f"{edit}?next={quote(reverse('workers:detail', args=[worker.pk]), safe='')}" in payout
    badge = block(html, "badge")
    assert "Active" in badge
    assert f"{reverse('checkin:badges')}?q={worker.number}" in badge
    assert f"{reverse('checkin:badge_sheet')}?worker={worker.pk}" in badge
    assert "4826" not in html


def test_statuses_without_anything_set_up(client, world):
    html = profile(client, world["admin"], world["worker"]).content.decode()

    assert "Messaging is off" in block(html, "messages")
    assert "No access" in block(html, "portal")
    assert "No payout account" in block(html, "payout")
    assert "No badge" in block(html, "badge")
    assert reverse("checkin:badge_sheet") not in block(html, "badge")


def test_the_accountant_sees_the_masked_account_without_an_edit_link(client, world):
    momo_account(world["worker"], number="+228 90 11 22 33")
    html = profile(client, world["accountant"], world["worker"]).content.decode()

    assert "•••• 2233" in html
    assert reverse("payouts:account_edit", args=[world["worker"].pk]) not in html


def test_the_foreman_gets_no_office_links(client, world):
    response = profile(client, world["foreman"], world["worker"])
    html = response.content.decode()

    assert response.status_code in (200, 403)
    assert reverse("messaging:worker", args=[world["worker"].pk]) not in html
    assert reverse("portal:office_worker", args=[world["worker"].pk]) not in html
    assert reverse("payouts:account_edit", args=[world["worker"].pk]) not in html
    assert not shown(html) & {"messages", "portal", "badge"}


def test_out_of_scope_profiles_show_nothing(client, world):
    assert profile(client, world["other_manager"], world["worker"]).status_code == 403
    with use_company(CompanyFactory()):
        stranger = AdminUserFactory()
    response = profile(client, stranger, world["worker"])
    assert response.status_code in (403, 404)
    assert not shown(response.content.decode())


def test_the_payout_account_form_returns_to_the_profile(client, world):
    worker = world["worker"]
    client.force_login(world["manager"])
    detail = reverse("workers:detail", args=[worker.pk])
    response = client.post(
        f"{reverse('payouts:account_edit', args=[worker.pk])}?next={detail}",
        {
            "method": "mobile_money",
            "mobile_operator": "Flooz",
            "mobile_number": "+228 90 11 22 33",
            "account_holder": "",
            "bank_name": "",
            "bank_code": "",
            "account_number": "",
            "next": detail,
        },
    )
    assert response.status_code == 302, response.content
    assert response.url == detail
