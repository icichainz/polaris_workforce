"""Several companies on one installation: each company's data is separate.

Companies A ("Alpha") and B ("Bravo") have the same structure: a site with a manager, a
foreman (PIN) and a worker with a photo; yesterday approved, paid with a proof and cash
issued; today submitted and corrected; an advance; an admin and an accountant. Every
name in B contains ``Bravo``. A's people — the admin included, who sees everything in A —
then try every office page and API endpoint (the URL classification of
``test_e2e_isolation.py``) on B's objects: 403/404, nothing changes, and no list, count,
dashboard, report, CSV export or audit log shows B. The tests also cover per-company
numbering and trades, phone numbers unique across companies, inactive companies, the
platform admin's switcher and the data migration from a single-company database.
"""

import datetime
from dataclasses import dataclass

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import ForemanDevice, User
from apps.accounts.permissions import scoped_sites
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog, Company, CompanySettings
from apps.core.tenancy import (
    SESSION_KEY,
    NoCurrentCompany,
    current_company,
    current_company_or_none,
    use_company,
)
from apps.payroll import services as payroll_services
from apps.payroll.models import PayLine, Payment
from apps.workers import services as worker_services
from apps.workers.models import Trade, Worker
from apps.worksites.models import SiteAssignment
from tests.e2e import Office, Phone, hire, lines_by_worker, make_site, new_uuid
from tests.e2e import record as mark
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    PlatformAdminFactory,
    SiteFactory,
    TradeFactory,
    UserFactory,
    WorkerFactory,
)
from tests.test_checkin_seed import seed_checkin
from tests.test_e2e_isolation import (
    API,
    JPEG,
    OFFICE,
    _admin_urls,
    _api_b_calls,
    _b_state,
    _list_pages,
    _office_b_get_urls,
    _office_b_posts,
    add_document,
    add_pay_extras,
    page_text,
)

pytestmark = pytest.mark.django_db

PIN = "9173"


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


@dataclass
class World:
    """One company with a day of data (see the module docstring)."""

    company: Company
    crew: object  # tests.e2e.Crew: site, manager, foreman, phone_number
    admin: User
    accountant: User
    worker: Worker
    ids: dict

    @property
    def site(self):  # so _b_state() takes a World
        return self.crew.site


def build_company(label: str, phone: str) -> World:
    company = CompanyFactory(name=f"{label} Construction", slug=label.lower())
    with use_company(company):
        CompanySettingsFactory(company_name=f"{label} Construction", working_days=[*range(1, 8)])
        today = payroll_services.company_today()
        yesterday = today - datetime.timedelta(days=1)
        crew = make_site(f"{label} Tower", phone_number=phone, pin=PIN)
        crew.manager.last_name = f"{label}manager"
        crew.manager.save()
        trade = TradeFactory(name=f"{label}trade")
        worker = hire(crew, "Zebulon", 6000, last_name=f"{label}worker", trade=trade)
        worker.photo.save("zeb.jpg", ContentFile(JPEG))
        document = add_document(worker, f"{label} contract")
        admin = AdminUserFactory(last_name=f"{label}admin")
        accountant = UserFactory(role=User.Role.ACCOUNTANT, last_name=f"{label}accountant")

    # The day goes through HTTP: each request works in its user's company.
    phone_app = Phone(phone, PIN)
    office = Office(crew.manager)
    put = phone_app.put_records(crew.site.pk, yesterday, [mark(worker, "present")])
    assert put.status_code == 200, put.content
    assert phone_app.submit(crew.site.pk, yesterday, new_uuid()).status_code == 200
    office.approve(AttendanceDay.objects.get(site=crew.site, date=yesterday).pk)
    office.issue_cash(crew.site.pk, yesterday, "10000")
    line = lines_by_worker(phone_app.payout(crew.site.pk, yesterday).json())[worker.pk]
    proof = SimpleUploadedFile("receipt.jpg", JPEG, content_type="image/jpeg")
    paid = phone_app.pay(line["pay_line_id"], line["net"], new_uuid(), "photo", proof)
    assert paid.status_code == 201, paid.content
    put = phone_app.put_records(crew.site.pk, today, [mark(worker, "late", "07:40")])
    assert phone_app.submit(crew.site.pk, today, new_uuid()).status_code == 200
    record_id = put.json()["records"][0]["id"]
    fix = phone_app.correct(record_id, new_uuid(), "arrival", "07:20", "Gate log says 07:20")
    assert fix.status_code == 201
    with use_company(company):
        payroll_services.create_advance(
            crew.manager, worker, amount=5000, deduction_per_pay=1000, proof_type="signature"
        )
        extras = add_pay_extras(crew, worker, today, label, label)

    ids = {
        **extras,
        "site": crew.site.pk,
        "yesterday": yesterday,
        "today": today,
        "pending_day": AttendanceDay.objects.get(site=crew.site, date=today).pk,
        "record": record_id,
        "line": PayLine.objects.get(worker=worker, period__end_date=yesterday).pk,
        "payment": Payment.objects.get(pay_line__worker=worker).pk,
        "worker": worker.pk,
        "document": document.pk,
        "assignment": SiteAssignment.objects.get(worker=worker).pk,
        "manager_b": crew.manager.pk,
        "foreman_b": crew.foreman.pk,
        "device_b": ForemanDevice.objects.get(user=crew.foreman).pk,
    }
    with use_company(company):
        ids.update(seed_checkin(crew, worker))
    return World(company, crew, admin, accountant, worker, ids)


@pytest.fixture
def worlds(db):
    a = build_company("Alpha", "+228 90 01 01 01")
    b = build_company("Bravo", "+228 90 02 02 02")
    return a, b


def _text(response) -> str:
    return page_text(response)


# --- office --------------------------------------------------------------------------------


def test_company_b_admin_sees_company_b(worlds):
    """Positive control: the URLs used below are valid and show B to B's own people."""
    _a, b = worlds
    office = Office(b.admin)
    for name, url in _office_b_get_urls(b.ids).items():
        assert office.get(url).status_code == 200, name
    for name, (url, query) in _list_pages(b.ids).items():
        response = office.get(url, query)
        assert response.status_code == 200, name
        if name not in (
            "workers:create",
            "workers:import",
            "dashboard:home",
            "payroll:advance_preview",
        ):
            assert "Bravo" in _text(response), name
    for name, url in _admin_urls(b.ids).items():
        if name not in ("accounts:user_set_pin", "accounts:device_revoke"):  # POST only
            assert office.get(url).status_code == 200, name


@pytest.mark.parametrize("who", ["admin", "accountant", "manager", "foreman"])
def test_company_a_people_cannot_reach_company_b_in_the_office(worlds, who):
    a, b = worlds
    user = {"manager": a.crew.manager, "foreman": a.crew.foreman}.get(who) or getattr(a, who)
    office = Office(user)
    before = _b_state(b)

    for name, url in _office_b_get_urls(b.ids).items():
        response = office.get(url)
        assert response.status_code in (403, 404), (name, response.status_code)

    for name, (url, data) in _office_b_posts(b.ids).items():
        for htmx in (False, True):
            response = office.post(url, data, htmx=htmx)
            assert response.status_code in (403, 404), (name, htmx, response.status_code)

    for name, url in _admin_urls(b.ids).items():
        post = "pin" in name or "revoke" in name
        response = (
            office.post(url, {"pin": "4827", "pin_confirm": "4827"}) if post else (office.get(url))
        )
        own_screens = (
            "core:settings",
            "core:audit_log",
            "worksites:create",
            "payroll:pay_rules",
            "payroll:deduction_rule_create",
            "payroll:task_type_create",
        )
        if who == "admin" and name in own_screens:
            assert response.status_code == 200, name  # A's own screens…
            assert "Bravo" not in _text(response), name  # …without B in them
        elif who == "admin" and name in (
            "accounts:users",
            "accounts:user_create",
            "messaging:messages",
            "messaging:settings",
            "messaging:recipients",
        ):
            assert response.status_code == 200, name
            assert "Bravo" not in _text(response), name
        else:
            assert response.status_code in (403, 404), (name, response.status_code)

    for name, (url, query) in _list_pages(b.ids).items():
        response = office.get(url, query)
        assert response.status_code in (200, 403), name
        assert "Bravo" not in _text(response), name
        if name == "payroll:advance_preview":
            assert response.context["preview"] is None

    # An advance for B's worker from A's screen is refused.
    response = office.post(
        reverse("payroll:advances"),
        {"worker": b.ids["worker"], "amount": "1000", "deduction_per_pay": "500"},
    )
    assert response.status_code in (200, 403)
    assert _b_state(b) == before
    assert ForemanDevice.objects.get(pk=b.ids["device_b"]).revoked_at is None
    assert User.objects.get(pk=b.ids["foreman_b"]).check_pin(PIN)


def test_counts_dashboards_and_reports_only_count_your_company(worlds):
    a, _b = worlds
    office = Office(a.admin)

    home = office.get(reverse("dashboard:home"))
    overview = home.context["overview"]
    assert [row.site.pk for row in overview.sites] == [a.site.pk]
    assert [day.pk for day in overview.awaiting] == [a.ids["pending_day"]]
    assert home.context["nav_badges"].approvals == 1  # A's pending day, not B's

    workers = office.get(reverse("workers:list"), {"status": "all"})
    assert workers.context["active_count"] == 1
    assert workers.context["inactive_count"] == 0

    sites = office.get(reverse("worksites:list"))
    assert [card["site"].pk for card in sites.context["cards"]] == [a.site.pk]

    span = {"period": "custom", "start": a.ids["yesterday"], "end": a.ids["today"]}
    report = office.get(reverse("dashboard:reports"), span)
    assert [row.worker_id for row in report.context["report"].workers] == [a.worker.pk]
    assert [row.site_id for row in report.context["report"].by_site] == [a.site.pk]
    csv = office.get(reverse("dashboard:reports_csv"), span)
    assert "Alphaworker" in _text(csv) and "Bravo" not in _text(csv)


def test_the_audit_log_shows_only_your_company(worlds):
    a, b = worlds
    assert AuditLog.objects.filter(company=b.company).exists()
    response = Office(a.admin).get(reverse("core:audit_log"))

    page = response.context["page"]
    assert page.paginator.count == AuditLog.objects.filter(company=a.company).count()
    assert {entry.company_id for entry in page} == {a.company.pk}
    # Filters only offer A's people.
    actors = set(response.context["form"].fields["actor"].queryset)
    assert actors and all(user.company_id == a.company.pk for user in actors)


def test_every_audit_entry_belongs_to_the_company_it_is_about(worlds):
    a, b = worlds
    assert not AuditLog.objects.filter(company__isnull=True).exists()
    for world in (a, b):
        actors = set(
            AuditLog.objects.filter(company=world.company)
            .exclude(actor=None)
            .values_list("actor__company", flat=True)
        )
        assert actors == {world.company.pk}


def test_media_files_of_another_company_are_404_for_a_company_admin(worlds):
    a, b = worlds
    proof = Payment.objects.get(pk=b.ids["payment"]).proof_file
    own_proof = Payment.objects.get(pk=a.ids["payment"]).proof_file

    assert Office(a.admin).get(proof.url).status_code == 404
    assert Office(a.admin).get(own_proof.url).status_code == 200
    assert Office(b.admin).get(proof.url).status_code == 200


# --- API -----------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["foreman_phone", "admin_session", "manager_session"])
def test_company_a_cannot_reach_company_b_on_the_api(worlds, who):
    a, b = worlds
    if who == "foreman_phone":
        client = Phone(a.crew.phone_number, PIN).client
    else:
        client = APIClient()
        client.force_login(a.admin if who == "admin_session" else a.crew.manager)
    before = _b_state(b)

    for method, url, data in _api_b_calls(b.ids):
        response = getattr(client, method)(url, data, format="json")
        assert response.status_code in (403, 404), (method, url, response.status_code)

    files = {
        "api:file-worker-photo": "worker",
        "api:file-payment-proof": "payment",
        "api:checkin:file-photo": "checkin",
    }
    for name, key in files.items():
        response = client.get(reverse(name, args=[b.ids[key]]))
        assert response.status_code == 404, name

    me = client.get("/api/v1/me/").json()
    sync = client.get("/api/v1/sync/").json()
    assert "Bravo" not in str(me) + str(sync)
    assert {s["id"] for s in me["sites"]} <= {a.site.pk}
    assert me["company"]["currency"]["code"] == "XOF"
    assert _b_state(b) == before


def test_api_classification_is_still_complete():
    """Every API URL is in test_e2e_isolation's table (which this module reuses)."""
    assert {"api:accounts:me", "api:attendance:sync", "api:payroll:payments"} <= set(API)
    assert OFFICE["core:companies"] == OFFICE["core:company_switch"] == "platform"


def test_each_phone_signs_in_to_its_own_company(worlds):
    a, b = worlds
    for world in (a, b):
        phone = Phone(world.crew.phone_number, PIN)
        assert [s["id"] for s in phone.sign_in["sites"]] == [world.site.pk]
        crew = phone.crew(world.site.pk, world.ids["today"]).json()
        assert [w["id"] for w in crew["workers"]] == [world.worker.pk]


def test_a_company_settings_change_stays_in_that_company(worlds):
    a, b = worlds
    Office(b.admin).save_settings(company_name="Bravo Construction", currency="EUR")

    assert CompanySettings.load(b.company).currency == "EUR"
    assert CompanySettings.load(a.company).currency == "XOF"
    me = Phone(a.crew.phone_number, PIN).me().json()
    assert me["company"]["currency"]["code"] == "XOF"


# --- per-company numbering, trades and phones ---------------------------------------------


def test_worker_numbers_are_a_sequence_per_company(default_company):
    other = CompanyFactory()
    mine = [worker_services.create_worker(first_name="A", last_name=str(n)) for n in range(2)]
    with use_company(other):
        theirs = worker_services.create_worker(first_name="B", last_name="0")

    assert [w.number for w in mine] == ["W-0001", "W-0002"]
    assert theirs.number == "W-0001"
    assert theirs.company == other
    with pytest.raises(IntegrityError):
        WorkerFactory(number="W-0001")  # taken in this company


def test_trade_names_are_per_company(default_company):
    other = CompanyFactory()
    mason = TradeFactory(name="Mason")
    with use_company(other):
        their_mason = TradeFactory(name="Mason")
    assert mason.pk != their_mason.pk
    with pytest.raises(IntegrityError):
        Trade.objects.create(company=default_company, name="Mason")


def test_worker_forms_only_offer_your_trades(default_company, client):
    mason = TradeFactory(name="Mason")
    with use_company(CompanyFactory()):
        TradeFactory(name="Bravo welder")
    client.force_login(AdminUserFactory())

    response = client.get(reverse("workers:create"))

    assert list(response.context["form"].fields["trade"].queryset) == [mason]
    assert "Bravo welder" not in response.content.decode()


def test_a_typed_trade_is_created_in_your_company(default_company):
    from apps.workers.forms import WorkerForm

    with use_company(CompanyFactory()):
        TradeFactory(name="Tiler")
    form = WorkerForm(data={}, sites=scoped_sites(AdminUserFactory()), decimals=0, today=None)
    form.cleaned_data = {"new_trade": "Tiler"}

    trade = form.trade_value()

    assert trade.company == default_company
    assert Trade.objects.filter(name="Tiler").count() == 2


def test_phone_numbers_are_unique_across_companies(worlds):
    a, b = worlds
    office = Office(a.admin)
    response = office.post(
        reverse("accounts:user_create"),
        {
            "first_name": "Copy",
            "last_name": "Cat",
            "username": "copycat",
            "phone": b.crew.phone_number.replace(" ", ""),
            "role": "foreman",
            "language": "",
        },
    )
    assert response.status_code == 200
    assert "phone" in response.context["form"].errors
    assert not User.objects.filter(username="copycat").exists()


def test_assigning_or_staffing_across_companies_is_refused(worlds):
    from django.core.exceptions import ValidationError

    from apps.worksites import services as site_services

    a, b = worlds
    with pytest.raises(ValidationError):
        site_services.assign(b.worker, a.site, a.ids["today"])
    a.site.manager = b.crew.manager
    with pytest.raises(ValidationError):
        site_services.save_site(a.site)


def test_new_users_join_the_admins_company(worlds):
    a, _b = worlds
    office = Office(a.admin)
    response = office.post(
        reverse("accounts:user_create"),
        {
            "first_name": "New",
            "last_name": "Foreman",
            "username": "newforeman",
            "phone": "+228 99 99 99 99",
            "role": "foreman",
            "language": "",
            "foreman_sites": [a.site.pk],
        },
    )
    assert response.status_code == 302
    assert User.objects.get(username="newforeman").company == a.company


# --- inactive companies -------------------------------------------------------------------


def test_an_inactive_company_cannot_sign_in(worlds):
    a, b = worlds
    phone = Phone(b.crew.phone_number, PIN)
    session = Client()
    session.force_login(b.admin)
    b.company.is_active = False
    b.company.save()

    assert phone.me().status_code == 401
    assert session.get(reverse("dashboard:home")).status_code == 302  # signed out
    again = APIClient().post(
        "/api/v1/auth/pin/",
        {"phone": b.crew.phone_number, "pin": PIN, "device_id": "x"},
        format="json",
    )
    assert again.status_code == 401
    b.admin.set_password("s3cret-pass")
    b.admin.save()
    assert not Client().login(username=b.admin.username, password="s3cret-pass")
    assert Phone(a.crew.phone_number, PIN).me().status_code == 200


# --- platform admin ---------------------------------------------------------------------------


def test_platform_admin_switches_between_companies(worlds):
    a, b = worlds
    platform = PlatformAdminFactory()
    office = Office(platform)

    # The first company by name is selected at first.
    home = office.get(reverse("worksites:list"))
    assert home.context["company_switcher"].current == a.company
    assert {c.pk for c in home.context["company_switcher"].companies} >= {
        a.company.pk,
        b.company.pk,
    }
    assert "Alpha Tower" in _text(home) and "Bravo Tower" not in _text(home)
    assert 'id="company-switcher"' in _text(home)

    response = office.post(reverse("core:company_switch"), {"company": b.company.pk})
    assert response.status_code == 302
    assert office.client.session[SESSION_KEY] == b.company.pk
    sites = office.get(reverse("worksites:list"))
    assert "Bravo Tower" in _text(sites) and "Alpha Tower" not in _text(sites)
    # Everything follows the selected company, including object pages.
    assert office.get(reverse("worksites:detail", args=[b.site.pk])).status_code == 200
    assert office.get(reverse("worksites:detail", args=[a.site.pk])).status_code == 403
    assert office.get(reverse("core:settings")).context["form"].instance.company == b.company
    users = office.get(reverse("accounts:users"))
    assert {row["user"].company_id for row in users.context["rows"]} == {b.company.pk}


def test_only_platform_admins_open_companies_and_the_switcher(worlds):
    a, b = worlds
    office = Office(a.admin)  # a superuser of company A
    assert office.get(reverse("core:companies")).status_code == 403
    response = office.post(reverse("core:company_switch"), {"company": b.company.pk})
    assert response.status_code == 403
    assert "company-switcher" not in _text(office.get(reverse("dashboard:home")))
    # The session key does nothing for a company user.
    session = office.client.session
    session[SESSION_KEY] = b.company.pk
    session.save()
    assert "Bravo" not in _text(office.get(reverse("worksites:list")))


def test_platform_admin_creates_a_company(db):
    Company.objects.all().delete()
    office = Office.__new__(Office)  # no company yet: every page leads to Companies
    office.user = PlatformAdminFactory()
    office.client = Client()
    office.client.force_login(office.user)

    response = office.client.get(reverse("dashboard:home"))
    assert response.status_code == 302
    assert response.url == reverse("core:companies")
    page = office.client.get(reverse("core:companies"))
    assert page.status_code == 200

    response = office.client.post(reverse("core:companies"), {"name": "Lagoon Builders"})
    assert response.status_code == 302
    company = Company.objects.get(name="Lagoon Builders")
    assert CompanySettings.objects.get(company=company).company_name == "Lagoon Builders"
    assert office.client.session[SESSION_KEY] == company.pk
    assert office.client.get(reverse("dashboard:home")).status_code == 200
    assert AuditLog.objects.get(action="core.company_create").company == company


def test_platform_admins_cannot_sign_in_on_the_phone(db):
    platform = PlatformAdminFactory(phone="+228 97 00 00 01")
    platform.set_pin("4827")
    platform.save()
    response = APIClient().post(
        "/api/v1/auth/pin/",
        {"phone": "+228 97 00 00 01", "pin": "4827", "device_id": "x"},
        format="json",
    )
    assert response.status_code == 401


# --- the current company --------------------------------------------------------------------


def test_code_without_a_current_company_fails_loudly(default_company):
    with use_company(None):
        assert current_company_or_none() is None
        with pytest.raises(NoCurrentCompany):
            current_company()
        with pytest.raises(NoCurrentCompany):
            worker_services.create_worker(first_name="No", last_name="Company")
        with pytest.raises(IntegrityError):
            SiteFactory.build(company=None, manager=None).save()  # refused by the database
    assert current_company() == default_company


def test_users_other_than_platform_admins_need_a_company(db):
    with use_company(None), pytest.raises(IntegrityError):
        ForemanFactory(company=None)


def test_a_request_puts_the_current_company_back(worlds):
    a, b = worlds
    with use_company(b.company):
        Office(a.admin).get(reverse("dashboard:home"))
        assert current_company() == b.company


# --- the data migration ----------------------------------------------------------------------

BEFORE_COMPANIES = [
    ("core", "0002_settings_display_and_holidays"),
    ("accounts", "0005_remove_foremandevice_pin_fields"),
    ("worksites", "0002_polaris_extra_fields"),
    ("workers", "0001_initial"),
]


def _migrate(targets):
    # Like `manage.py migrate`: no current company (the `company` columns' default reads it).
    with use_company(None):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
    return executor


@pytest.mark.django_db(transaction=True)
def test_the_migration_puts_existing_data_in_a_default_company():
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    executor = _migrate(BEFORE_COMPANIES)
    old = executor.loader.project_state(BEFORE_COMPANIES).apps
    try:
        old.get_model("core", "CompanySettings").objects.create(
            pk=1, company_name="Acme Build", currency="EUR", currency_decimals=2
        )
        users = old.get_model("accounts", "User").objects
        boss = users.create(username="boss", is_superuser=True, is_staff=True, role="admin")
        users.create(username="kofi", role="foreman", phone="+22890000001")
        site = old.get_model("worksites", "Site").objects.create(name="Old Site")
        trade = old.get_model("workers", "Trade").objects.create(name="Mason")
        old.get_model("workers", "Worker").objects.create(
            number="W-0001", first_name="Ama", last_name="Old", trade=trade
        )
        old.get_model("core", "AuditLog").objects.create(actor=boss, action="settings.update")
    finally:
        _migrate(latest)

    company = Company.objects.get()
    assert (company.name, company.slug, company.is_active) == ("Acme Build", "acme-build", True)
    settings_row = CompanySettings.objects.get()
    assert settings_row.company == company
    assert (settings_row.currency, settings_row.currency_decimals) == ("EUR", 2)
    assert set(User.objects.values_list("username", "company")) == {
        ("boss", company.pk),
        ("kofi", company.pk),
    }
    assert list(Trade.objects.values_list("company", flat=True)) == [company.pk]
    assert list(Worker.objects.values_list("company", flat=True)) == [company.pk]
    assert list(AuditLog.objects.values_list("company", flat=True)) == [company.pk]
    from apps.worksites.models import Site

    assert Site.objects.get(pk=site.pk).company == company


@pytest.mark.django_db(transaction=True)
def test_the_migration_leaves_an_empty_database_without_companies():
    Company.objects.all().delete()
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        _migrate(BEFORE_COMPANIES)
    finally:
        _migrate(latest)

    assert not Company.objects.exists()
    assert not CompanySettings.objects.exists()
