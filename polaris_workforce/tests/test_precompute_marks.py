"""The precompute service: what each change marks out of date, and what a rebuild clears.

``apps.precompute``: every audit entry marks the sites and dates it changes
(``marks.ranges_for``), in the change's own transaction; ``ensure_fresh`` and the cron
command rebuild only what is marked (and each site once on a new day).
"""

import datetime
import pathlib
import re

import pytest
from django.apps import apps as django_apps
from django.contrib.admin.sites import site as admin_site
from django.db import transaction
from django.test import RequestFactory
from django.urls import reverse

from apps.attendance import services as attendance
from apps.core.models import AuditLog
from apps.precompute import marks, services
from apps.precompute.models import Freshness, RefreshRun, Stale
from apps.workers import services as workers
from apps.worksites import services as worksites
from apps.worksites.models import Site
from tests.factories import CompanySettingsFactory, SiteFactory
from tests.test_payroll_services import (  # noqa: F401
    MON,
    SUBMITTED,
    TUE,
    company,
    hire,
    roll_call,
    setup_site,
)

APPS_DIR = pathlib.Path(__file__).resolve().parent.parent / "apps"


def stale(site) -> tuple | None:
    row = Stale.objects.filter(site=site).first()
    return (row.start, row.end) if row else None


# --- every action is accounted for ------------------------------------------------------


def _actions_in_the_code() -> set[str]:
    """Dotted string literals that look like audit actions, model labels left out."""
    labels = {model._meta.label_lower for model in django_apps.get_models()}
    labels |= {"auth.group", "auth.permission"}
    found = set()
    pattern = re.compile(
        r'"((?:payroll|payouts|accounts|portal|attendance|checkin|workers|worksites|'
        r'messaging|agents|core|settings)\.[a-z_.]+)"'
    )
    for path in APPS_DIR.rglob("*.py"):
        parts = path.relative_to(APPS_DIR).parts
        # Celery task names (tasks.py) look like actions but are not.
        if "migrations" in parts or parts[0] == "precompute" or path.name == "tasks.py":
            continue
        found |= set(pattern.findall(path.read_text()))
    return found - labels


def test_every_audit_action_is_classified():
    known = marks.IGNORED | marks.COMPANY | marks.BY_TARGET
    actions = _actions_in_the_code()
    assert len(actions) > 50
    unknown = sorted(a for a in actions if a not in known and not marks.is_ignored(a))
    assert not unknown, (
        f"Audit actions apps.precompute.marks does not know: {unknown}. Add each to "
        "IGNORED (changes no figure), COMPANY (every site) or BY_TARGET (and its "
        "target model to _PLACERS)."
    )


def test_the_lists_do_not_overlap():
    assert not marks.IGNORED & marks.COMPANY
    assert not marks.IGNORED & marks.BY_TARGET
    assert not marks.COMPANY & marks.BY_TARGET


# --- what a change marks ----------------------------------------------------------------


def test_a_roll_call_marks_its_site_and_day(company):  # noqa: F811
    site, manager, _foreman = setup_site()
    other, *_ = setup_site()
    day = roll_call(site, MON, {hire(site): "present"}, status=SUBMITTED)

    attendance.approve_day(day, manager)

    assert stale(site)[0] == MON
    assert stale(other) is None


def test_marks_of_a_site_merge(company):  # noqa: F811
    site, *_ = setup_site()
    services.mark(site.pk, TUE, TUE)
    services.mark(site.pk, MON, MON)
    assert stale(site) == (MON, TUE)
    services.mark(site.pk, None, TUE)
    assert stale(site) == (None, TUE)
    assert Stale.objects.get(site=site).version == 3


def test_a_rate_change_marks_the_workers_sites_from_its_date(company):  # noqa: F811
    site, manager, _foreman = setup_site()
    elsewhere, *_ = setup_site()
    worker = hire(site)

    workers.change_rate(worker, 7000, "daily", TUE, actor=manager)

    assert stale(site)[0] == TUE
    assert stale(elsewhere) is None


def test_site_settings_mark_every_open_period_of_the_site(company):  # noqa: F811
    site, manager, _foreman = setup_site()
    worksites.update_site_settings(site, {"late_after": datetime.time(8, 0)}, actor=manager)
    assert stale(site)[0] is None


def test_company_rules_mark_every_site_with_pay(company):  # noqa: F811
    sites = [setup_site()[0] for _ in range(2)]
    completed = SiteFactory(status=Site.Status.COMPLETED)
    AuditLog.record(actor=None, action="settings.update")
    for site in sites:
        assert stale(site)[0] is None
    assert stale(completed) is None  # no open period: nothing to rebuild


def test_sign_ins_and_the_systems_own_recomputation_mark_nothing(company):  # noqa: F811
    site, *_ = setup_site()
    AuditLog.record(actor=None, action="accounts.login_locked")
    AuditLog.record(actor=None, action="payroll.auto.period.refresh", target=site)
    AuditLog.record(actor=None, action="payroll.period.status", target=site)
    assert not Stale.objects.exists()


def test_a_change_that_rolls_back_leaves_no_mark(company):  # noqa: F811
    site, *_ = setup_site()
    with pytest.raises(RuntimeError), transaction.atomic():
        AuditLog.record(actor=None, action="worksites.settings_update", target=site)
        raise RuntimeError
    assert not Stale.objects.exists()


def test_an_unknown_target_marks_the_whole_company(company):  # noqa: F811
    site, *_ = setup_site()
    other_company_object = CompanySettingsFactory()
    AuditLog.record(actor=None, action="workers.update", target=other_company_object)
    assert stale(site)[0] is None


def test_admin_edits_are_audited_and_mark(company, admin_user):  # noqa: F811
    from apps.worksites.models import SiteAssignment

    site, *_ = setup_site()
    worker = hire(site)
    assignment = SiteAssignment.objects.get(worker=worker)
    assignment_pk = assignment.pk
    model_admin = admin_site._registry[SiteAssignment]
    request = RequestFactory().post("/admin/")
    request.user = admin_user

    model_admin.delete_model(request, assignment)

    entry = AuditLog.objects.get(action="admin.worksites.siteassignment.delete")
    assert entry.object_id == str(assignment_pk)
    assert entry.company_id == site.company_id
    assert stale(site)[0] == datetime.date(2025, 1, 1)


# --- rebuilding ---------------------------------------------------------------------------


@pytest.fixture
def recorded_steps(monkeypatch):
    """Replace the rebuild steps with one that records what it was asked."""
    calls = []

    def step(site, start, end, **_options):
        calls.append((site.pk, start, end))

    monkeypatch.setattr(services, "_STEPS", [(0, step)])
    return calls


def test_a_read_rebuilds_a_marked_site_once(company, recorded_steps):  # noqa: F811
    site, *_ = setup_site()
    services.mark(site.pk, MON, MON)

    assert services.ensure_fresh(site, MON, MON, today=TUE)
    # Never rebuilt: the mark, and the periods a new day may have made ready.
    lookback = TUE - datetime.timedelta(days=services.NEW_DAY_LOOKBACK)
    assert recorded_steps == [(site.pk, lookback, TUE)]
    assert not Stale.objects.exists()
    assert Freshness.objects.get(site=site).fresh_on == TUE

    assert not services.ensure_fresh(site, MON, MON, today=TUE)
    assert len(recorded_steps) == 1


def test_a_mark_on_other_dates_does_not_hold_up_a_read(company, recorded_steps):  # noqa: F811
    site, *_ = setup_site()
    Freshness.objects.create(site=site, fresh_on=TUE)
    services.mark(site.pk, datetime.date(2025, 6, 2), datetime.date(2025, 6, 2))

    assert not services.ensure_fresh(site, MON, TUE, today=TUE)
    assert recorded_steps == []
    assert services.ensure_fresh(site, None, None, today=TUE)  # a read of every date


def test_a_new_day_rebuilds_since_the_last_one(company, recorded_steps):  # noqa: F811
    site, *_ = setup_site()
    Freshness.objects.create(site=site, fresh_on=MON)

    assert services.ensure_fresh(site, TUE, TUE, today=TUE)
    assert recorded_steps == [(site.pk, MON, TUE)]


def test_a_mark_made_during_a_rebuild_is_kept(company, monkeypatch):  # noqa: F811
    site, *_ = setup_site()
    services.mark(site.pk, MON, MON)

    def step(site, start, end, **_options):
        services.mark(site.pk, TUE, TUE)  # a write lands while the rebuild runs

    monkeypatch.setattr(services, "_STEPS", [(0, step)])
    services.ensure_fresh(site, MON, MON, today=TUE)

    assert stale(site) == (MON, TUE)


def test_the_cron_run_rebuilds_marked_and_not_yet_fresh_sites(
    company,  # noqa: F811
    recorded_steps,
):
    marked, *_ = setup_site()
    fresh, *_ = setup_site()
    Freshness.objects.create(site=fresh, fresh_on=TUE)
    Freshness.objects.create(site=marked, fresh_on=TUE)
    services.mark(marked.pk, MON, MON)

    run = services.run(marked.company_id, today=TUE)

    assert [pk for pk, *_ in recorded_steps] == [marked.pk]
    assert run.sites == 1 and run.finished_at is not None
    assert not Stale.objects.exists()
    assert RefreshRun.objects.count() == 1


def test_the_command_runs_for_every_active_company(company, recorded_steps):  # noqa: F811
    from django.core.management import call_command

    site, *_ = setup_site()
    services.mark(site.pk, MON, MON)
    call_command("refresh_precomputed", verbosity=0)
    assert site.pk in [pk for pk, *_ in recorded_steps]
    assert not Stale.objects.filter(site=site).exists()


@pytest.fixture
def admin_user(db):
    from tests.factories import UserFactory

    return UserFactory(is_superuser=True, is_staff=True, company=None)


def test_the_admin_change_page_still_saves(company, client, admin_user):  # noqa: F811
    site, *_ = setup_site()
    client.force_login(admin_user)
    url = reverse("admin:worksites_site_change", args=[site.pk])
    assert client.get(url).status_code == 200
