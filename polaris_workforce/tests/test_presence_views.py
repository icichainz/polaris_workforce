"""Presence office screens (brief P4): setup, BLE badges, live map, timeline, export,
notice, the dashboard-token view, and the cards on the site and worker pages.

What must hold:
- Admins and site managers only (accountants and foremen: 403; signed out: sign-in, or
  401 JSON for the token view). A site manager sees their own sites only; another site
  answers 403 and its zones, gateways, badges and workers 404; another company's data is
  never reached, not even by its ids.
- Every change goes through the services (audited) and the gateway token is shown once,
  on a page that is not cached.
- The browser's tracker token covers the user's tracked sites only (``travel`` for
  admins), and names on the live map come from Django, for the people the user may see.
- The pages render in English and in French, without English left on the French ones.
"""

import csv
import datetime
import io
import json
import re
from types import SimpleNamespace

import pytest
from django.conf import settings as django_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.checkin.models import SiteGeofence
from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence import selectors, tokens
from apps.presence.models import (
    BleBadge,
    Gateway,
    PresenceReconciliation,
    PresenceSession,
    Zone,
)
from apps.presence.views import tracker_public_url
from tests import test_i18n_fr as i18n_fr  # as a module: its autouse fixtures stay there
from tests.e2e import Office, hire, make_site
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    SiteAssignmentFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db

PUBLIC = "http://localhost:8080/tracker"


@pytest.fixture(autouse=True)
def _tracker(settings):
    """A public tracker URL with a path prefix (as behind nginx), and no push URL: no
    test here talks to a tracker unless it says so."""
    settings.TRACKER_PUBLIC_URL = PUBLIC
    settings.TRACKER_URL = ""


@pytest.fixture
def world(default_company):
    """Company A with tracking on: North (tracked, geofence, zone, gateway, badge) and
    Hilltop (another manager's site, tracking off there)."""
    company = CompanySettingsFactory()
    company.tracking_enabled = True
    company.save()
    north = make_site("North", phone_number="+228 90 11 22 33")
    north.yao = hire(north, "Yao", 5000, last_name="Mensah")
    north.ama = hire(north, "Ama", 5000, last_name="Agbo")
    SiteGeofence.objects.create(site=north.site, latitude="6.131900", longitude="1.222800")
    north.zone = Zone.objects.create(site=north.site, code="GATE", name="Main gate")
    north.gateway = Gateway.objects.create(site=north.site, zone=north.zone, label="Gate box")
    north.badge = BleBadge.objects.create(
        company_id=north.site.company_id, worker=north.yao, badge_id="ib:abc:1:2"
    )
    hill = make_site("Hilltop", phone_number="+228 90 44 55 66", tracking_enabled=False)
    hill.zeb = hire(hill, "Zeb", 5000, last_name="Hilltopworker")
    hill.zone = Zone.objects.create(site=hill.site, code="H", name="Hill zone")
    hill.gateway = Gateway.objects.create(site=hill.site, label="Hill box")
    hill.badge = BleBadge.objects.create(
        company_id=hill.site.company_id, worker=hill.zeb, badge_id="hill-1"
    )
    admin = AdminUserFactory(first_name="Edem", last_name="Admin")
    return SimpleNamespace(company=company, north=north, hill=hill, admin=admin)


@pytest.fixture
def bravo(db):
    """Another company, with its own tracked site and data."""
    company = CompanyFactory(name="Bravo Construction", slug="bravo")
    with use_company(company):
        CompanySettingsFactory(tracking_enabled=True)
        crew = make_site("Bravo Tower", phone_number="+228 90 02 02 02")
        worker = hire(crew, "Zed", 5000, last_name="Bravoworker")
        admin = AdminUserFactory(last_name="Bravoadmin")
        zone = Zone.objects.create(site=crew.site, code="Z", name="Bravo zone")
        gateway = Gateway.objects.create(site=crew.site, label="Bravo gate")
        badge = BleBadge.objects.create(company_id=company.pk, worker=worker, badge_id="bravo-1")
    return SimpleNamespace(
        company=company, crew=crew, worker=worker, admin=admin, zone=zone, gateway=gateway,
        badge=badge,
    )  # fmt: skip


def session(site, worker, start, end=None, *, source="live", zones=("GATE",), kind="worker"):
    tz = datetime.UTC
    return PresenceSession.objects.create(
        company_id=site.company_id,
        site=site,
        subject_kind=kind,
        subject_id=worker.pk,
        local_date=start.astimezone(tz).date(),
        started_at=start,
        ended_at=end,
        end_reason="timeout" if end else "",
        zones=list(zones),
        last_event_at=end or start,
        event_count=2,
        source=source,
    )


def at(day, hour, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=datetime.UTC)


def pages(w):
    n = w.north
    today = company_today()
    return {
        "home": reverse("presence:home"),
        "badges": reverse("presence:badges"),
        "notice": reverse("presence:notice"),
        "site_setup": reverse("presence:site_setup", args=[n.site.pk]),
        "live": reverse("presence:live", args=[n.site.pk]),
        "zone_create": reverse("presence:zone_create", args=[n.site.pk]),
        "zone_edit": reverse("presence:zone_edit", args=[n.zone.pk]),
        "gateway_create": reverse("presence:gateway_create", args=[n.site.pk]),
        "gateway_edit": reverse("presence:gateway_edit", args=[n.gateway.pk]),
        "gateway_token": reverse("presence:gateway_token", args=[n.gateway.pk]),
        "worker_badge": reverse("presence:worker_badge", args=[n.yao.pk]),
        "person_day": reverse("presence:person_day", args=["worker", n.yao.pk, today]),
        "worker_export": reverse("presence:worker_export", args=[n.yao.pk]),
    }


# --- who may open the pages ----------------------------------------------------------------


@pytest.mark.parametrize("who", ["admin", "manager"])
def test_pages_open_for_admins_and_the_sites_manager(world, who):
    office = Office(world.admin if who == "admin" else world.north.manager)
    for name, url in pages(world).items():
        assert office.get(url).status_code == 200, name


@pytest.mark.parametrize("role", [User.Role.ACCOUNTANT, User.Role.FOREMAN])
def test_pages_are_closed_to_accountants_and_foremen(world, role):
    office = Office(UserFactory(role=role))
    for name, url in pages(world).items():
        assert office.get(url).status_code == 403, name
    assert office.get(reverse("presence:dashboard_token")).status_code == 403


def test_signed_out_goes_to_sign_in_and_the_token_view_answers_401(world, client):
    for name, url in pages(world).items():
        response = client.get(url)
        assert response.status_code == 302 and "/accounts/login/" in response["Location"], name
    response = client.get(reverse("presence:dashboard_token"))
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_another_sites_manager_is_refused_everywhere(world):
    office = Office(world.hill.manager)
    n = world.north
    for url in [
        reverse("presence:site_setup", args=[n.site.pk]),
        reverse("presence:live", args=[n.site.pk]),
        reverse("presence:zone_create", args=[n.site.pk]),
        reverse("presence:gateway_create", args=[n.site.pk]),
    ]:
        assert office.get(url).status_code == 403, url
    assert office.post(reverse("presence:site_push", args=[n.site.pk])).status_code == 403
    for url in [
        reverse("presence:zone_edit", args=[n.zone.pk]),
        reverse("presence:gateway_edit", args=[n.gateway.pk]),
        reverse("presence:gateway_token", args=[n.gateway.pk]),
        reverse("presence:worker_badge", args=[n.yao.pk]),
        reverse("presence:worker_export", args=[n.yao.pk]),
        reverse("presence:person_day", args=["worker", n.yao.pk, company_today()]),
    ]:
        assert office.get(url).status_code == 404, url
    for url in [
        reverse("presence:zone_delete", args=[n.zone.pk]),
        reverse("presence:gateway_token", args=[n.gateway.pk]),
        reverse("presence:gateway_revoke", args=[n.gateway.pk]),
        reverse("presence:badge_end", args=[n.badge.pk]),
    ]:
        assert office.post(url).status_code == 404, url
    assert Zone.objects.filter(pk=n.zone.pk).exists()
    n.gateway.refresh_from_db()
    assert n.gateway.token_issued_at is None
    n.badge.refresh_from_db()
    assert n.badge.active_to is None
    assert not AuditLog.objects.filter(action__startswith="presence.").exists()


def test_lists_show_only_the_managers_own_sites_and_workers(world):
    office = Office(world.north.manager)
    home = office.get(reverse("presence:home"))
    assert [row.site.pk for row in home.context["rows"]] == [world.north.site.pk]
    badges = office.get(reverse("presence:badges")).content.decode()
    assert "Mensah" in badges and "Hilltopworker" not in badges and "hill-1" not in badges


def test_another_company_is_never_reached(world, bravo):
    office = Office(world.admin)
    for url in [
        reverse("presence:site_setup", args=[bravo.crew.site.pk]),
        reverse("presence:live", args=[bravo.crew.site.pk]),
    ]:
        assert office.get(url).status_code == 403, url
    for url in [
        reverse("presence:zone_edit", args=[bravo.zone.pk]),
        reverse("presence:gateway_token", args=[bravo.gateway.pk]),
        reverse("presence:worker_badge", args=[bravo.worker.pk]),
        reverse("presence:worker_export", args=[bravo.worker.pk]),
        reverse("presence:person_day", args=["worker", bravo.worker.pk, company_today()]),
        reverse("presence:person_day", args=["foreman", bravo.crew.foreman.pk, company_today()]),
    ]:
        assert office.get(url).status_code == 404, url
    assert office.post(reverse("presence:badge_end", args=[bravo.badge.pk])).status_code == 404
    assert (
        office.post(reverse("presence:gateway_token", args=[bravo.gateway.pk])).status_code == 404
    )
    home = office.get(reverse("presence:home")).content.decode()
    assert "Bravo Tower" not in home
    claims = _claims(office.get(reverse("presence:dashboard_token")).json()["token"])
    assert bravo.crew.site.pk not in claims["sites"]
    assert claims["company"] == world.north.site.company_id


def test_sidebar_entry_for_admins_and_managers_only(world):
    href = f'href="{reverse("presence:home")}"'
    assert href in Office(world.admin).get("/").content.decode()
    assert href in Office(world.north.manager).get("/").content.decode()
    accountant = Office(UserFactory(role=User.Role.ACCOUNTANT))
    assert href not in accountant.get("/").content.decode()


# --- zones -----------------------------------------------------------------------------------


def test_zone_create_edit_delete_go_through_the_services(world):
    office = Office(world.north.manager)
    site = world.north.site
    response = office.post(
        reverse("presence:zone_create", args=[site.pk]), {"code": "B", "name": "Block B"}
    )
    assert response.status_code == 302
    zone = Zone.objects.get(site=site, code="B")
    response = office.post(
        reverse("presence:zone_edit", args=[zone.pk]), {"code": "B2", "name": "Block B"}
    )
    assert response.status_code == 302
    zone.refresh_from_db()
    assert zone.code == "B2"
    assert office.post(reverse("presence:zone_delete", args=[zone.pk])).status_code == 302
    assert not Zone.objects.filter(pk=zone.pk).exists()
    actions = list(
        AuditLog.objects.filter(action__startswith="presence.zone").values_list("action", flat=True)
    )
    assert sorted(actions) == [
        "presence.zone.create",
        "presence.zone.delete",
        "presence.zone.update",
    ]


def test_zone_form_refuses_a_taken_or_malformed_code(world):
    office = Office(world.north.manager)
    url = reverse("presence:zone_create", args=[world.north.site.pk])
    taken = office.post(url, {"code": "GATE", "name": "Again"})
    assert taken.status_code == 200
    assert "code" in taken.context["form"].errors
    bad = office.post(url, {"code": "a b!", "name": "Bad"})
    assert "code" in bad.context["form"].errors
    assert Zone.objects.filter(site=world.north.site).count() == 1
    # The same code on another site is fine.
    hill = Office(world.hill.manager).post(
        reverse("presence:zone_create", args=[world.hill.site.pk]), {"code": "GATE", "name": "G"}
    )
    assert hill.status_code == 302


# --- gateways and their tokens -----------------------------------------------------------------


def _claims(token):
    return tokens.decode(token, audience=tokens.TRACKER_AUDIENCE)


def test_new_gateway_leads_to_its_token_shown_once(world):
    office = Office(world.north.manager)
    site = world.north.site
    response = office.post(
        reverse("presence:gateway_create", args=[site.pk]),
        {"label": "Block B box", "zone": world.north.zone.pk},
    )
    gateway = Gateway.objects.get(label="Block B box")
    assert response.status_code == 302
    assert response["Location"] == reverse("presence:gateway_token", args=[gateway.pk])

    confirm = office.get(response["Location"])
    assert confirm.status_code == 200 and confirm.context.get("token") is None
    issued = office.post(response["Location"])
    assert issued.status_code == 200
    token = issued.context["token"]
    html = issued.content.decode()
    assert token in html and html.count(token) == 1
    assert "no-store" in issued["Cache-Control"]
    gateway.refresh_from_db()
    claims = _claims(token)
    assert claims["kind"] == "gateway" and claims["sites"] == [site.pk]
    assert claims["sub"] == f"gateway:{gateway.tracker_id}"
    assert f"{PUBLIC}/v1/gateways/sightings" in html
    # The token is not kept anywhere: the setup page does not show it.
    setup = office.get(reverse("presence:site_setup", args=[site.pk])).content.decode()
    assert token not in setup and gateway.tracker_id in setup


def test_reissue_and_revoke_are_audited(world):
    office = Office(world.admin)
    gateway = world.north.gateway
    url = reverse("presence:gateway_token", args=[gateway.pk])
    first = office.post(url).context["token"]
    second = office.post(url).context["token"]
    assert first != second
    gateway.refresh_from_db()
    assert gateway.token_generation == 2
    response = office.post(
        reverse("presence:gateway_revoke", args=[gateway.pk]), {"reason": "Stolen"}
    )
    assert response.status_code == 302
    gateway.refresh_from_db()
    assert gateway.revoked_at is not None and gateway.revoked_by == world.admin
    revoke = AuditLog.objects.get(action="presence.gateway.revoke")
    assert revoke.reason == "Stolen"
    assert AuditLog.objects.filter(action="presence.gateway.token_issue").count() == 2


def test_gateway_zone_must_be_of_its_site(world):
    office = Office(world.admin)
    response = office.post(
        reverse("presence:gateway_create", args=[world.north.site.pk]),
        {"label": "Wrong", "zone": world.hill.zone.pk},
    )
    assert response.status_code == 200
    assert "zone" in response.context["form"].errors
    assert not Gateway.objects.filter(label="Wrong").exists()


def test_gateway_label_is_required(world):
    office = Office(world.admin)
    response = office.post(
        reverse("presence:gateway_edit", args=[world.north.gateway.pk]), {"label": ""}
    )
    assert "label" in response.context["form"].errors


def test_site_push_sends_the_config_when_a_tracker_is_set(world, settings, monkeypatch):
    sent = []
    monkeypatch.setattr("apps.presence.services.queue_site_push", sent.extend)
    office = Office(world.north.manager)
    url = reverse("presence:site_push", args=[world.north.site.pk])
    assert office.post(url).status_code == 302
    assert sent == []  # no TRACKER_URL: nothing to send to
    settings.TRACKER_URL = "http://tracker:4100"
    assert office.post(url).status_code == 302
    assert sent == [world.north.site.pk]


# --- BLE badges ------------------------------------------------------------------------------


def test_assign_normalizes_and_ends_the_previous_badge(world):
    office = Office(world.north.manager)
    yao = world.north.yao
    response = office.post(
        reverse("presence:worker_badge", args=[yao.pk]),
        {"badge_id": "  IB:F7826DA6-4FA2-4E98-8024-BC5B71E0893E:100:7 "},
    )
    assert response.status_code == 302
    active = BleBadge.objects.get(worker=yao, active_to__isnull=True)
    assert active.badge_id == "ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7"
    world.north.badge.refresh_from_db()
    assert world.north.badge.active_to is not None
    assert AuditLog.objects.filter(action="presence.badge.assign").exists()


def test_a_badge_held_by_another_worker_is_refused(world):
    office = Office(world.admin)
    response = office.post(
        reverse("presence:worker_badge", args=[world.north.ama.pk]), {"badge_id": "IB:ABC:1:2"}
    )
    assert response.status_code == 200
    errors = response.context["form"].errors["badge_id"]
    assert "Yao Mensah" in " ".join(errors)
    assert not BleBadge.objects.filter(worker=world.north.ama).exists()


def test_badge_form_validation(world):
    office = Office(world.admin)
    url = reverse("presence:worker_badge", args=[world.north.ama.pk])
    assert "badge_id" in office.post(url, {"badge_id": "   "}).context["form"].errors
    assert "badge_id" in office.post(url, {"badge_id": "x" * 65}).context["form"].errors


def test_end_badge(world):
    office = Office(world.north.manager)
    response = office.post(
        reverse("presence:badge_end", args=[world.north.badge.pk]),
        {"next": reverse("presence:badges")},
    )
    assert response.status_code == 302 and response["Location"] == reverse("presence:badges")
    world.north.badge.refresh_from_db()
    assert world.north.badge.active_to is not None
    assert AuditLog.objects.filter(action="presence.badge.end").count() == 1


def test_badges_search_finds_a_badge_id(world):
    response = Office(world.admin).get(reverse("presence:badges"), {"q": "IB:ABC"})
    assert [row["worker"].pk for row in response.context["rows"]] == [world.north.yao.pk]


# --- the browser's tracker token ------------------------------------------------------------


def test_dashboard_token_covers_the_users_tracked_sites(world):
    manager = Office(world.north.manager).get(reverse("presence:dashboard_token"))
    assert manager.status_code == 200
    assert "no-store" in manager["Cache-Control"]
    body = manager.json()
    assert body["tracker_url"] == PUBLIC
    claims = _claims(body["token"])
    assert claims["kind"] == "dashboard" and claims["sites"] == [world.north.site.pk]
    assert claims["travel"] is False
    assert body["expires_at"] == claims["exp"]

    admin = _claims(Office(world.admin).get(reverse("presence:dashboard_token")).json()["token"])
    assert admin["sites"] == [world.north.site.pk]  # Hilltop is not tracked
    assert admin["travel"] is True


def test_dashboard_token_without_a_public_tracker(world, settings):
    settings.TRACKER_PUBLIC_URL = ""
    response = Office(world.admin).get(reverse("presence:dashboard_token"))
    assert response.status_code == 503
    assert response.json()["code"] == "tracker_not_configured"


def test_dashboard_token_is_get_only(world):
    assert Office(world.admin).post(reverse("presence:dashboard_token")).status_code == 405


def test_tracker_urls_keep_the_path_prefix(settings):
    settings.TRACKER_PUBLIC_URL = "http://localhost:8080/tracker/"
    assert tracker_public_url() == "http://localhost:8080/tracker"
    assert tracker_public_url("/v1/gateways/sightings") == (
        "http://localhost:8080/tracker/v1/gateways/sightings"
    )
    settings.TRACKER_PUBLIC_URL = ""
    assert tracker_public_url("v1/x") == ""


# --- live map ----------------------------------------------------------------------------------


def _config(response) -> dict:
    match = re.search(r'data-config="([^"]*)"', response.content.decode())
    assert match, "no data-config on the page"
    import html

    return json.loads(html.unescape(match.group(1)))


def test_live_map_config_has_django_names_for_the_people_the_user_may_see(world):
    response = Office(world.north.manager).get(reverse("presence:live", args=[world.north.site.pk]))
    assert "no-store" in response["Cache-Control"]
    config = _config(response)
    assert config["siteId"] == world.north.site.pk
    assert config["trackerUrl"] == PUBLIC
    assert config["tokenUrl"] == reverse("presence:dashboard_token")
    assert config["geofence"]["radius"] == 150.0
    assert config["zones"] == {"GATE": "Main gate"}
    people = config["people"]
    assert people[f"worker:{world.north.yao.pk}"]["name"] == "Yao Mensah"
    assert f"worker:{world.hill.zeb.pk}" not in people  # another manager's worker
    foreman = people[f"foreman:{world.north.foreman.pk}"]
    assert foreman["url"] == ""  # foremen's timelines: admins only
    assert _claims(config["token"])["sites"] == [world.north.site.pk]
    assert 'x-data="pageModule"' in response.content.decode()


def test_live_map_for_an_admin_links_foremen_and_names_badge_holders(world):
    hill_worker = world.hill.zeb
    config = _config(Office(world.admin).get(reverse("presence:live", args=[world.north.site.pk])))
    assert config["people"][f"worker:{hill_worker.pk}"]["name"] == "Zeb Hilltopworker"
    assert config["people"][f"foreman:{world.north.foreman.pk}"]["url"]


def test_live_map_degrades_when_tracking_is_off_or_no_tracker(world, settings):
    office = Office(world.admin)
    hill = office.get(reverse("presence:live", args=[world.hill.site.pk]))
    html = hill.content.decode()
    assert "Tracking is off for this site." in html and "data-config" not in html
    settings.TRACKER_PUBLIC_URL = ""
    north = office.get(reverse("presence:live", args=[world.north.site.pk])).content.decode()
    assert "TRACKER_PUBLIC_URL" in north and "data-config" not in north


def test_live_map_lists_todays_visits_from_django(world):
    today = company_today()
    session(world.north.site, world.north.yao, at(today, 7, 2), at(today, 12, 30))
    html = (
        Office(world.north.manager)
        .get(reverse("presence:live", args=[world.north.site.pk]))
        .content.decode()
    )
    assert "Yao Mensah" in html and "07:02" in html and "12:30" in html


def test_live_map_page_has_every_connection_message(world):
    html = (
        Office(world.admin)
        .get(reverse("presence:live", args=[world.north.site.pk]))
        .content.decode()
    )
    for state in [
        "connecting",
        "live",
        "reconnecting",
        "unreachable",
        "not_configured",
        "forbidden",
        "expired",
        "rejected",
    ]:
        assert f'data-status="{state}"' in html, state
    assert "<template data-person-row>" in html


# --- a person's day ---------------------------------------------------------------------------


def test_timeline_shows_sessions_and_marks_replayed_ones(world):
    today = company_today()
    yao = world.north.yao
    session(world.north.site, yao, at(today, 7, 0), at(today, 11, 0))
    session(world.north.site, yao, at(today, 12, 0), at(today, 16, 30), source="replay")
    response = Office(world.north.manager).get(
        reverse("presence:person_day", args=["worker", yao.pk, today])
    )
    html = response.content.decode()
    rows = response.context["timeline"].rows
    assert [r.minutes for r in rows] == [240, 270]
    assert html.count("Corrected by the nightly check") == 1
    assert "no-store" in response["Cache-Control"]
    config = _config(response)
    assert config["kind"] == "worker" and config["id"] == yao.pk
    assert config["from"] == f"{today.isoformat()}T00:00:00Z"
    assert "Trips between sites are shown to company administrators only." in html


def test_timeline_hides_sessions_on_sites_out_of_scope(world):
    today = company_today()
    zeb = world.hill.zeb
    SiteAssignmentFactory(worker=zeb, site=world.north.site, start_date=today)
    session(world.hill.site, zeb, at(today, 7, 0), at(today, 9, 0))
    session(world.north.site, zeb, at(today, 10, 0), at(today, 11, 0))
    response = Office(world.north.manager).get(
        reverse("presence:person_day", args=["worker", zeb.pk, today])
    )
    assert [r.session.site_id for r in response.context["timeline"].rows] == [world.north.site.pk]


def test_foreman_timeline_is_for_admins_only(world):
    foreman = world.north.foreman
    url = reverse("presence:person_day", args=["foreman", foreman.pk, company_today()])
    assert Office(world.north.manager).get(url).status_code == 403
    response = Office(world.admin).get(url)
    assert response.status_code == 200
    assert "trips between sites included" in response.content.decode()


def test_timeline_without_date_shows_today_and_the_picker_redirects(world):
    office = Office(world.admin)
    url = reverse("presence:person", args=["worker", world.north.yao.pk])
    today = office.get(url)
    assert today.status_code == 200 and today.context["date"] == company_today()
    picked = office.get(url, {"date": "2026-10-30"})
    assert picked["Location"].endswith("/2026-10-30/")
    assert office.get(reverse("presence:person", args=["robot", 1])).status_code == 404


def test_timeline_has_no_track_beyond_retention(world):
    old = company_today() - datetime.timedelta(days=60)
    response = Office(world.admin).get(
        reverse("presence:person_day", args=["worker", world.north.yao.pk, old])
    )
    html = response.content.decode()
    assert "data-config" not in html and "Raw positions are deleted after 30 days" in html


def test_timeline_bars_cover_tracking_hours_and_late_visits(world):
    company = CompanySettings.load()
    day = datetime.date(2026, 10, 30)
    yao = world.north.yao
    session(world.north.site, yao, at(day, 6, 0), at(day, 12, 0))
    session(world.north.site, yao, at(day, 20, 0), at(day, 21, 30))
    rows = list(PresenceSession.objects.order_by("started_at"))
    line = selectors.timeline(rows, day, company)
    # 06:00 → 22:00: the tracking hours (06–19) widened to hold the 21:30 departure.
    assert line.ticks[0] == ("06:00", 0.0) and line.ticks[-1] == ("22:00", 100.0)
    assert line.rows[0].left == 0.0 and line.rows[0].width == 37.5
    assert line.rows[1].left == 87.5
    assert line.total_minutes == 450


# --- the worker's presence history -------------------------------------------------------------


def test_export_lists_the_workers_sessions_on_the_users_sites(world):
    today = company_today()
    zeb = world.hill.zeb
    zeb.first_name = "=HYPERLINK(1)"
    zeb.save()
    SiteAssignmentFactory(worker=zeb, site=world.north.site, start_date=today)
    session(world.north.site, zeb, at(today, 7, 0), at(today, 9, 15), zones=("GATE", "B"))
    session(world.north.site, zeb, at(today, 10, 0), source="replay")
    session(world.hill.site, zeb, at(today, 12, 0), at(today, 13, 0))

    response = Office(world.north.manager).get(reverse("presence:worker_export", args=[zeb.pk]))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert response["Content-Disposition"] == f'attachment; filename="presence-{zeb.number}.csv"'
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0][:4] == ["Worker number", "Worker", "Date", "Site"]
    assert len(rows) == 3  # Hilltop's visit is not the North manager's to see
    first, second = rows[1], rows[2]
    assert first[1].startswith("'")  # no formula in a spreadsheet
    assert first[3:9] == [
        "North",
        f"{today.isoformat()} 07:00",
        f"{today.isoformat()} 09:15",
        "135",
        "GATE > B",
        "Not seen for a while",
    ]
    assert first[9] == "Live events"
    assert second[5:7] == ["", ""] and second[9] == "Nightly replay"

    admin = Office(world.admin).get(reverse("presence:worker_export", args=[zeb.pk]))
    assert len(list(csv.reader(io.StringIO(admin.content.decode().lstrip("﻿"))))) == 4


# --- the printed notice -----------------------------------------------------------------------


def test_notice_in_english_and_french_on_a4_and_letter(world):
    office = Office(world.north.manager)
    english = office.get(reverse("presence:notice")).content.decode()
    assert "Presence tracking on our sites" in english and "size: A4" in english
    assert "06:00–19:00" in english and "after 30 days" in english
    assert django_settings.TRACKING_CONSENT_TEXT_VERSION in english

    french = office.get(reverse("presence:notice"), {"lang": "fr", "paper": "letter"})
    html = french.content.decode()
    assert "Le suivi de présence sur nos chantiers" in html and "size: letter" in html
    assert 'lang="fr"' in html


def test_notice_hours_of_a_site_and_out_of_scope_site_ignored(world):
    site = world.north.site
    site.tracking_start = datetime.time(7, 30)
    site.save()
    office = Office(world.north.manager)
    own = office.get(reverse("presence:notice"), {"site": site.pk}).content.decode()
    assert "07:30–19:00" in own and "These are the hours of North." in own
    other = office.get(reverse("presence:notice"), {"site": world.hill.site.pk}).content.decode()
    assert "Hilltop" not in other and "06:00–19:00" in other


# --- cards on other pages ----------------------------------------------------------------------


def test_cards_on_the_site_and_worker_pages(world):
    office = Office(world.north.manager)
    site_page = office.get(reverse("worksites:detail", args=[world.north.site.pk])).content.decode()
    assert 'id="site-presence"' in site_page
    assert reverse("presence:live", args=[world.north.site.pk]) in site_page
    worker_page = office.get(reverse("workers:detail", args=[world.north.yao.pk])).content.decode()
    assert 'id="worker-presence"' in worker_page and "ib:abc:1:2" in worker_page
    assert reverse("presence:worker_export", args=[world.north.yao.pk]) in worker_page

    accountant = Office(UserFactory(role=User.Role.ACCOUNTANT))
    page = accountant.get(reverse("worksites:detail", args=[world.north.site.pk])).content.decode()
    assert 'id="site-presence"' not in page
    page = accountant.get(reverse("workers:detail", args=[world.north.yao.pk])).content.decode()
    assert 'id="worker-presence"' not in page


# --- tracker sync and nightly check on the setup page ------------------------------------------


def test_setup_page_shows_sync_and_the_latest_nightly_check(world, settings):
    site = world.north.site
    office = Office(world.admin)
    url = reverse("presence:site_setup", args=[site.pk])
    html = office.get(url).content.decode()
    assert "No tracker" in html and "Not run yet" in html

    settings.TRACKER_URL = "http://tracker:4100"
    now = timezone.now()
    from apps.presence.models import TrackerSync

    TrackerSync.objects.create(site=site, last_attempt_at=now, last_error="HTTP 503")
    PresenceReconciliation.objects.create(
        company_id=site.company_id,
        site=site,
        local_date=company_today() - datetime.timedelta(days=2),
        reconciled_at=now,
        last_attempt_at=now,
        added=1,
        changed=2,
        removed=0,
        unchanged=7,
    )
    html = office.get(url).content.decode()
    assert "Last push failed" in html and "HTTP 503" in html
    assert "Checked" in html and "1 added · 2 changed · 0 removed · 7 unchanged" in html

    PresenceReconciliation.objects.create(
        company_id=site.company_id,
        site=site,
        local_date=company_today() - datetime.timedelta(days=1),
        last_attempt_at=now,
        last_error="HTTP 404 site_not_configured",
        attempts=3,
    )
    html = office.get(url).content.decode()
    assert "Last check failed" in html and "HTTP 404 site_not_configured" in html
    assert "(3 attempts)" in html


# --- French -------------------------------------------------------------------------------------


EXPECTED_FR = {
    "home": ["Présence", "Carte en direct"],
    "badges": ["Badges BLE"],
    "site_setup": ["Passerelles", "Contrôle de nuit"],
    "live": ["Sur le chantier", "Vus aujourd’hui"],
    "zone_create": ["Nouvelle zone"],
    "gateway_create": ["Nouvelle passerelle"],
    "gateway_token": ["Émettre un jeton"],
    "worker_badge": ["Badge BLE de"],
    "person_day": ["Trace brute", "Passages"],
}


def test_pages_render_in_french_without_english_left(world):
    office = Office(world.admin)
    office.client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = "fr"
    today = company_today()
    session(world.north.site, world.north.yao, at(today, 7, 0), at(today, 9, 0), source="replay")
    PresenceReconciliation.objects.create(
        company_id=world.north.site.company_id,
        site=world.north.site,
        local_date=today - datetime.timedelta(days=1),
        reconciled_at=timezone.now(),
        last_attempt_at=timezone.now(),
    )
    english = i18n_fr._english_phrases()
    for name, url in pages(world).items():
        if name == "worker_export":
            continue
        response = office.get(url)
        assert response.status_code == 200, name
        text = i18n_fr._visible_text(response.content.decode())
        for phrase in EXPECTED_FR.get(name, []):
            assert phrase in text, (name, phrase)
        left = [phrase for phrase in english if phrase in text]
        assert not left, (name, left[:5])
