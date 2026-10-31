"""Check-in office pages: overview, a site's day, badges, print sheet, kiosks, geofences."""

import datetime
import uuid
from decimal import Decimal

import pytest
import segno
from django.urls import reverse

from apps.accounts.models import ForemanDevice, User
from apps.checkin import services
from apps.checkin.models import Badge, KioskDevice, SiteGeofence
from apps.checkin.views import qr_path
from apps.core.dates import company_today
from apps.core.models import AuditLog
from tests.e2e import Office, admin_office, hire, make_site
from tests.factories import CompanySettingsFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def crew():
    CompanySettingsFactory()
    crew = make_site("Riverside", phone_number="+228 90 11 22 33")
    crew.ama = hire(crew, "Ama", 5000, last_name="Mensah")
    crew.kofi = hire(crew, "Kofi", 5000, last_name="Agbo")
    crew.device = ForemanDevice.objects.create(
        user=crew.foreman, device_id="gate", name="Gate phone", token_hash="g" * 64
    )
    return crew


@pytest.fixture
def other():
    other = make_site("Hilltop", phone_number="+228 90 44 55 66")
    other.zeb = hire(other, "Zeb", 5000, last_name="Hilltopworker")
    return other


ALL_PAGES = [
    "checkin:home",
    "checkin:badges",
    "checkin:badge_sheet",
    "checkin:kiosks",
    "checkin:geofences",
]


@pytest.mark.parametrize("name", ALL_PAGES)
def test_pages_open_for_admin_and_manager(crew, name):
    for user in (UserFactory(role=User.Role.ADMIN), crew.manager):
        assert Office(user).get(reverse(name)).status_code == 200, (name, user.role)


@pytest.mark.parametrize("role", [User.Role.FOREMAN, User.Role.ACCOUNTANT])
def test_pages_closed_for_foremen_and_accountants(crew, role):
    office = Office(UserFactory(role=role))
    for name in ALL_PAGES:
        assert office.get(reverse(name)).status_code == 403, name


def test_sidebar_entry_only_for_admins_and_managers(crew):
    href = f'href="{reverse("checkin:home")}"'
    assert href in Office(crew.manager).get("/").content.decode()
    assert href in admin_office().get("/").content.decode()
    accountant = Office(UserFactory(role=User.Role.ACCOUNTANT))
    assert href not in accountant.get("/").content.decode()


# --- badges ------------------------------------------------------------------------------------


def test_issue_reissue_and_revoke_from_the_office(crew):
    office = Office(crew.manager)
    issue = reverse("checkin:badge_issue", args=[crew.ama.pk])

    assert office.post(issue).status_code == 302
    first = Badge.objects.get(worker=crew.ama)
    assert office.post(issue).status_code == 302
    first.refresh_from_db()
    assert first.revoked_at is not None
    response = office.post(
        reverse("checkin:badge_revoke", args=[crew.ama.pk]), {"reason": "Lost on site"}
    )
    assert response.status_code == 302
    assert not Badge.objects.filter(worker=crew.ama, revoked_at__isnull=True).exists()
    actions = list(
        AuditLog.objects.filter(action__startswith="checkin.badge").values_list("action", flat=True)
    )
    assert sorted(actions) == sorted(
        ["checkin.badge.issue", "checkin.badge.issue", "checkin.badge.revoke"]
    )


def test_issue_missing_badges_for_a_site(crew):
    services.issue_badge(crew.ama, crew.manager)
    office = Office(crew.manager)

    response = office.post(reverse("checkin:badges_issue_missing", args=[crew.site.pk]))

    assert response.status_code == 302
    assert Badge.objects.filter(revoked_at__isnull=True).count() == 2


def test_badges_list_shows_status(crew):
    services.issue_badge(crew.ama, crew.manager)
    response = Office(crew.manager).get(reverse("checkin:badges"), {"site": crew.site.pk})
    rows = {row["worker"].pk: row["badge"] for row in response.context["rows"]}
    assert rows[crew.ama.pk] is not None and rows[crew.kofi.pk] is None
    assert response.context["missing"] == 1


def test_manager_cannot_touch_other_sites_workers(crew, other):
    office = Office(crew.manager)
    assert office.post(reverse("checkin:badge_issue", args=[other.zeb.pk])).status_code == 404
    assert office.post(reverse("checkin:badge_revoke", args=[other.zeb.pk])).status_code == 404
    url = reverse("checkin:badges_issue_missing", args=[other.site.pk])
    assert office.post(url).status_code == 403
    assert not Badge.objects.exists()
    page = office.get(reverse("checkin:badges")).content.decode()
    assert "Hilltopworker" not in page


def test_badge_sheet_prints_ten_per_page_with_scannable_qr(crew):
    for n in range(10):
        services.issue_badge(hire(crew, f"W{n}", 5000), crew.manager)
    services.issue_badge(crew.ama, crew.manager)  # 11 badges in all

    response = Office(crew.manager).get(reverse("checkin:badge_sheet"), {"site": crew.site.pk})

    assert response.status_code == 200
    assert [len(page) for page in response.context["pages"]] == [10, 1]
    html = response.content.decode()
    assert html.count("<svg viewBox=") >= 11
    assert "Mensah" in html and crew.ama.number in html
    assert "|safe" not in html and "<script>alert" not in html


def test_badge_sheet_for_one_worker_and_scope(crew, other):
    badge = services.issue_badge(crew.ama, crew.manager)
    services.issue_badge(other.zeb, other.manager)
    office = Office(crew.manager)

    one = office.get(reverse("checkin:badge_sheet"), {"worker": crew.ama.pk})
    assert [c["badge"] for page in one.context["pages"] for c in page] == [badge]
    stranger = office.get(reverse("checkin:badge_sheet"), {"worker": other.zeb.pk})
    assert stranger.context["pages"] == []
    assert office.get(reverse("checkin:badge_sheet"), {"site": other.site.pk}).status_code == 403


def test_qr_path_draws_the_segno_matrix():
    payload = "PW1:K7Q2M4XR5TBN3WCA.D6HZP2LQ4V7NYE3S"
    size, path = qr_path(payload)
    qr = segno.make_qr(payload, error="m")
    assert qr.mode == "alphanumeric"
    assert size == qr.symbol_size(scale=1, border=4)[0]
    dark = sum(sum(1 for cell in row if cell) for row in qr.matrix)
    drawn = sum(int(part.split("h")[1].split("v")[0]) for part in path.split("z") if part)
    assert drawn == dark
    assert set(path) <= set("0123456789Mhvz,-")


# --- a site's day ---------------------------------------------------------------------------


def test_site_day_lists_check_ins_with_flags_and_suggestions(crew):
    day = company_today() - datetime.timedelta(days=1)
    at = datetime.datetime.combine(day, datetime.time(7, 30), tzinfo=datetime.UTC)
    services.record_checkin(
        user=crew.foreman,
        device=None,
        site=crew.site,
        direction="in",
        at=at,
        client_uuid=uuid.uuid4(),
        worker=crew.ama,
    )
    services.record_checkin(
        user=crew.foreman,
        device=None,
        site=crew.site,
        direction="in",
        at=at,
        client_uuid=uuid.uuid4(),
        badge="garbage",
    )

    response = Office(crew.manager).get(reverse("checkin:site_day", args=[crew.site.pk, day]))

    assert response.status_code == 200
    assert response.context["flagged"] == 1
    assert [s["s"].status for s in response.context["suggestions"]] == ["late"]
    html = response.content.decode()
    assert "Unknown badge" in html and "garbage" in html


def test_day_picker_redirects(crew):
    day = company_today()
    response = Office(crew.manager).get(
        reverse("checkin:home"), {"site": crew.site.pk, "date": day.isoformat()}
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("checkin:site_day", args=[crew.site.pk, day])


def test_site_day_of_another_site_is_403(crew, other):
    url = reverse("checkin:site_day", args=[other.site.pk, company_today()])
    assert Office(crew.manager).get(url).status_code == 403


# --- kiosks ---------------------------------------------------------------------------------


def test_make_and_end_kiosk(crew):
    office = Office(crew.manager)
    page = office.get(reverse("checkin:kiosks"))
    assert [row["device"] for row in page.context["rows"]] == [crew.device]

    response = office.post(
        reverse("checkin:kiosk_set", args=[crew.device.pk]),
        {f"d{crew.device.pk}-site": crew.site.pk},
    )
    assert response.status_code == 302
    assert KioskDevice.objects.get().site == crew.site
    assert "Kiosk · Riverside" in office.get(reverse("checkin:kiosks")).content.decode()

    assert office.post(reverse("checkin:kiosk_end", args=[crew.device.pk])).status_code == 302
    assert not KioskDevice.objects.exists()


def test_kiosk_for_a_site_the_foreman_is_not_on_is_refused(crew, other):
    office = Office(admin_office().user)
    response = office.post(
        reverse("checkin:kiosk_set", args=[crew.device.pk]),
        {f"d{crew.device.pk}-site": other.site.pk},
    )
    assert response.status_code == 302
    assert not KioskDevice.objects.exists()


def test_other_managers_cannot_see_or_switch_a_phone(crew, other):
    office = Office(other.manager)
    assert office.get(reverse("checkin:kiosks")).context["rows"] == []
    url = reverse("checkin:kiosk_set", args=[crew.device.pk])
    assert office.post(url, {f"d{crew.device.pk}-site": other.site.pk}).status_code == 404
    assert not KioskDevice.objects.exists()


def test_signed_out_phones_are_not_listed(crew):
    crew.device.revoked_at = crew.device.created_at
    crew.device.save()
    assert Office(crew.manager).get(reverse("checkin:kiosks")).context["rows"] == []


# --- geofences -----------------------------------------------------------------------------


def test_set_edit_and_remove_a_geofence(crew):
    office = Office(crew.manager)
    url = reverse("checkin:geofence", args=[crew.site.pk])
    assert office.get(url).status_code == 200

    data = {"latitude": "6.131944", "longitude": "1.222778", "radius_m": "150"}
    assert office.post(url, data).status_code == 302
    fence = SiteGeofence.objects.get(site=crew.site)
    assert (fence.latitude, fence.radius_m) == (Decimal("6.131944"), 150)
    assert office.get(url).context["form"].initial["radius_m"] == 150

    assert office.post(url, {"action": "clear"}).status_code == 302
    assert not SiteGeofence.objects.exists()


@pytest.mark.parametrize(
    "data",
    [
        {"latitude": "91", "longitude": "1", "radius_m": "150"},
        {"latitude": "6", "longitude": "181", "radius_m": "150"},
        {"latitude": "6", "longitude": "1", "radius_m": "5"},
        {"latitude": "6", "longitude": "1", "radius_m": "6000"},
        {"latitude": "", "longitude": "1", "radius_m": "150"},
    ],
)
def test_bad_geofences_are_refused(crew, data):
    response = Office(crew.manager).post(reverse("checkin:geofence", args=[crew.site.pk]), data)
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not SiteGeofence.objects.exists()


def test_geofence_of_another_site_is_403(crew, other):
    url = reverse("checkin:geofence", args=[other.site.pk])
    office = Office(crew.manager)
    assert office.get(url).status_code == 403
    assert (
        office.post(url, {"latitude": "1", "longitude": "1", "radius_m": "100"}).status_code == 403
    )
