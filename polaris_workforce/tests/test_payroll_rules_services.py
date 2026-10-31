"""Pay rules services (Wave 4): rules read by the pay, the day extras (piece work,
bonuses, stoppage days), their audit, permissions, read-only paid/locked pay, the
employer cost for reports and the isolation between companies.

Dates are in September 2025 (1 Sep 2025 is a Monday; 7 Sep a Sunday), like
``test_payroll_services``.
"""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.api.exceptions import Locked
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.payroll import pay_rules, services
from apps.payroll.models import (
    Bonus,
    DeductionRule,
    PayLine,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    TaskType,
)
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    UserFactory,
)
from tests.test_payroll_services import MON, TUE, hire, line_of, roll_call, setup_site

SUN = MON + datetime.timedelta(days=6)
SUBMITTED = AttendanceDay.Status.SUBMITTED


@pytest.fixture
def company(db):
    return CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])


@pytest.fixture
def crew(company):
    site, manager, foreman = setup_site()
    yao = hire(site, 6000, first_name="Yao")
    return site, manager, foreman, yao


def admin():
    return AdminUserFactory()


def task(name="Block laying", rate=50, unit="block"):
    return TaskType.objects.create(name=name, unit=unit, unit_rate=rate)


def deduction(name="Social security", employee="5", employer="10", **kwargs):
    kwargs.setdefault("active_from", datetime.date(2025, 1, 1))
    return DeductionRule.objects.create(
        name=name,
        employee_percent=Decimal(employee),
        employer_percent=Decimal(employer),
        **kwargs,
    )


def rules(**values):
    settings = PayRuleSettings.load()
    for name, value in values.items():
        setattr(settings, name, value)
    settings.save()
    return settings


def refreshed_line(site, date, worker):
    services.cash_sheet(site, date)
    return line_of(site, date, worker)


def actions():
    return list(AuditLog.objects.values_list("action", flat=True))


# --- rules read by the pay -------------------------------------------------------------------


def test_default_rules_change_nothing(crew):
    site, _m, _f, yao = crew
    roll_call(site, SUN, {yao: "present"})

    line = refreshed_line(site, SUN, yao)

    assert (line.gross, line.net, line.statutory_deductions, line.deductions) == (6000, 6000, 0, [])
    assert "multiplier" not in line.days[0]


def test_sunday_multiplier_and_the_site_override(crew):
    site, _m, _f, yao = crew
    other, _m2, _f2 = setup_site()
    afi = hire(other, 6000)
    rules(sunday_multiplier=Decimal("2"))
    SitePayRules.objects.create(site=site, sunday_multiplier=Decimal("3"))
    roll_call(site, SUN, {yao: "present"})
    roll_call(other, SUN, {afi: "present"})

    assert refreshed_line(site, SUN, yao).gross == 18000
    assert refreshed_line(other, SUN, afi).gross == 12000
    assert line_of(site, SUN, yao).days[0]["day_kind"] == "sunday"


def test_public_holiday_multiplier_reads_the_company_holidays(crew, company):
    site, _m, _f, yao = crew
    company.public_holidays = [{"date": MON.isoformat(), "name": "Independence"}]
    company.save()
    rules(holiday_multiplier=Decimal("1.5"))
    roll_call(site, MON, {yao: "present"})
    roll_call(site, TUE, {yao: "present"})

    assert refreshed_line(site, MON, yao).gross == 9000
    assert refreshed_line(site, TUE, yao).gross == 6000


def test_night_window_from_the_rules_and_the_site(crew):
    site, _m, _f, _yao = crew
    esi = hire(site, 600, pay_type="hourly")
    rules(
        night_start=datetime.time(22), night_end=datetime.time(6), night_multiplier=Decimal("1.5")
    )
    roll_call(site, MON, {esi: ("present", datetime.time(18), datetime.time(2))})

    assert refreshed_line(site, MON, esi).gross == 4200 + 1200
    SitePayRules.objects.create(
        site=site, night_start=datetime.time(0), night_end=datetime.time(5)
    )  # a site night of 00:00–05:00: 2 h of the shift
    assert refreshed_line(site, MON, esi).gross == 4200 + 600


def test_statutory_deductions_on_the_line_and_the_sheet(crew):
    site, _m, _f, yao = crew
    afi = hire(site, 3500)
    deduction()  # 5 % employee, 10 % employer
    deduction("Hourly levy", employee="2", employer="0", pay_types=["hourly"])
    roll_call(site, MON, {yao: "present", afi: "half_day"})

    sheet = services.cash_sheet(site, MON)
    yao_line, afi_line = line_of(site, MON, yao), line_of(site, MON, afi)

    assert (yao_line.gross, yao_line.statutory_deductions, yao_line.net) == (6000, 300, 5700)
    assert yao_line.employer_contributions == 600
    assert [d["name"] for d in yao_line.deductions] == ["Social security"]
    assert (afi_line.gross, afi_line.statutory_deductions, afi_line.net) == (1750, 88, 1662)
    assert sheet.reconciliation.total_to_pay == 5700 + 1662


def test_deduction_rules_follow_their_dates(crew):
    site, _m, _f, yao = crew
    deduction(active_from=TUE)
    roll_call(site, MON, {yao: "present"})
    roll_call(site, TUE, {yao: "present"})

    assert refreshed_line(site, MON, yao).statutory_deductions == 0
    assert refreshed_line(site, TUE, yao).statutory_deductions == 300


def test_another_companys_rules_never_touch_this_pay(crew):
    site, _m, _f, yao = crew
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        deduction("Their tax", employee="50")
        rules(sunday_multiplier=Decimal("5"))
        task("Their task")
    roll_call(site, SUN, {yao: "present"})

    line = refreshed_line(site, SUN, yao)

    assert (line.gross, line.net) == (6000, 6000)
    assert list(pay_rules.company_rules(admin())[1]) == []


def test_a_paid_line_keeps_the_rules_it_was_paid_with(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})
    line = refreshed_line(site, MON, yao)
    services.record_payment(manager, line, amount=6000, proof_type="signature")
    deduction()

    assert refreshed_line(site, MON, yao).net == 6000


def test_locking_unpaid_keeps_the_statutory_deduction(crew):
    site, _m, _f, yao = crew
    deduction()
    roll_call(site, MON, {yao: "present"})
    services.cash_sheet(site, MON)

    services.lock_sheet(admin(), site, MON, "Yao left")

    line = line_of(site, MON, yao)
    assert (line.statutory_deductions, line.net) == (300, 5700)


# --- stoppage days -----------------------------------------------------------------------


def test_stoppage_day_pays_the_share_and_is_audited(crew):
    site, manager, _f, yao = crew
    rules(stoppage_percent=Decimal("40"))
    roll_call(site, MON, {yao: "present"})

    stoppage = pay_rules.set_stoppage(manager, site, MON, "Heavy rain")

    assert (stoppage.pay, stoppage.percent) == ("percent", Decimal("40"))
    assert line_of(site, MON, yao).gross == 2400
    assert line_of(site, MON, yao).days[0]["stoppage"] is True
    entry = AuditLog.objects.get(action="payroll.stoppage.set")
    assert (entry.actor, entry.reason, entry.after["percent"]) == (manager, "Heavy rain", "40")

    pay_rules.set_stoppage(manager, site, MON, "Heavy rain", pay="fixed", amount=1500)
    assert line_of(site, MON, yao).gross == 1500

    assert pay_rules.clear_stoppage(manager, site, MON, "Rain stopped at 8")
    assert line_of(site, MON, yao).gross == 6000
    assert "payroll.stoppage.clear" in actions()
    assert not pay_rules.clear_stoppage(manager, site, MON)


def test_stoppage_needs_a_reason_and_a_valid_share(crew):
    site, manager, _f, _yao = crew
    with pytest.raises(ValidationError):
        pay_rules.set_stoppage(manager, site, MON, "  ")
    with pytest.raises(ValidationError):
        pay_rules.set_stoppage(manager, site, MON, "Rain", percent=Decimal("120"))
    with pytest.raises(ValidationError):
        pay_rules.set_stoppage(manager, site, MON, "Rain", pay="fixed", amount=-1)
    assert not StoppageDay.objects.exists()


def test_a_paid_or_locked_day_cannot_get_a_stoppage(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})
    line = refreshed_line(site, MON, yao)
    services.record_payment(manager, line, amount=6000, proof_type="signature")

    with pytest.raises(Locked):
        pay_rules.set_stoppage(manager, site, MON, "Rain")

    services.lock_sheet(admin(), site, MON)
    with pytest.raises(Locked):
        pay_rules.set_stoppage(manager, site, MON, "Rain")
    assert pay_rules.day_extras(site, MON).is_open is False


# --- piece work ----------------------------------------------------------------------------


def test_piece_work_is_paid_once_the_day_is_approved(crew):
    site, manager, _f, yao = crew
    blocks = task(rate=50)
    roll_call(site, MON, {yao: "present"}, status=SUBMITTED)

    entry = pay_rules.add_piece_work(manager, site, yao, MON, blocks, Decimal("120"), "Wall A")

    assert entry.unit_rate == 50 and entry.entered_by == manager
    assert entry.day == AttendanceDay.objects.get(site=site, date=MON)
    assert line_of(site, MON, yao).gross == 6000  # preview: the day is not approved yet
    AttendanceDay.objects.filter(site=site, date=MON).update(status=AttendanceDay.Status.APPROVED)
    line = refreshed_line(site, MON, yao)
    assert (line.gross, line.piece_amount) == (12000, 6000)
    assert line.pieces[0]["task"] == "Block laying"
    assert "payroll.piece.create" in actions()


def test_a_piece_rate_worker_is_paid_by_the_piece_only(crew):
    site, manager, _f, _yao = crew
    kofi = hire(site, 5000, pay_type="piece")
    roll_call(site, MON, {kofi: "present"})
    services.cash_sheet(site, MON)
    assert not PayLine.objects.filter(worker=kofi).exists() or line_of(site, MON, kofi).gross == 0

    pay_rules.add_piece_work(manager, site, kofi, MON, task(rate=400), Decimal("12.5"))

    line = line_of(site, MON, kofi)
    assert (line.gross, line.piece_amount, line.units) == (5000, 5000, Decimal("1.00"))


def test_task_rate_changes_do_not_reprice_entries(crew):
    site, manager, _f, yao = crew
    blocks = task(rate=50)
    roll_call(site, MON, {yao: "present"})
    pay_rules.add_piece_work(manager, site, yao, MON, blocks, Decimal("10"))
    blocks.unit_rate = 80
    blocks.save()

    assert refreshed_line(site, MON, yao).piece_amount == 500


def test_piece_work_checks_the_worker_and_the_task(crew):
    site, manager, _f, yao = crew
    other, _m, _f2 = setup_site()
    stranger = hire(other, 6000)
    old = task("Old task")
    old.is_active = False
    old.save()
    with use_company(CompanyFactory()):
        theirs = task("Their task")

    with pytest.raises(ValidationError) as error:
        pay_rules.add_piece_work(manager, site, stranger, MON, task(), Decimal("1"))
    assert "worker" in error.value.message_dict
    for bad_task in (old, theirs):
        with pytest.raises(ValidationError) as error:
            pay_rules.add_piece_work(manager, site, yao, MON, bad_task, Decimal("1"))
        assert "task" in error.value.message_dict
    with pytest.raises(ValidationError):
        pay_rules.add_piece_work(manager, site, yao, MON, task("T2"), Decimal("0"))
    assert not PieceWorkEntry.objects.exists()


def test_piece_work_is_removed_until_the_pay_is_paid(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})
    entry = pay_rules.add_piece_work(manager, site, yao, MON, task(), Decimal("10"))
    pay_rules.delete_piece_work(manager, entry, "Typed twice")
    assert line_of(site, MON, yao).gross == 6000
    assert AuditLog.objects.get(action="payroll.piece.delete").reason == "Typed twice"

    entry = pay_rules.add_piece_work(manager, site, yao, MON, task("T2"), Decimal("10"))
    line = line_of(site, MON, yao)
    services.record_payment(manager, line, amount=line.net, proof_type="signature")
    with pytest.raises(Locked):
        pay_rules.delete_piece_work(manager, entry)
    with pytest.raises(Locked):
        pay_rules.add_piece_work(manager, site, yao, MON, task("T3"), Decimal("1"))


# --- bonuses -------------------------------------------------------------------------------


def test_a_bonus_goes_on_the_next_pay(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})

    bonus = pay_rules.add_bonus(manager, site, yao, MON, 2500, "Finished the slab early")

    bonus.refresh_from_db()
    line = line_of(site, MON, yao)
    assert bonus.applied_to == line
    assert (line.gross, line.bonus_amount, line.net) == (8500, 2500, 8500)
    assert {"payroll.bonus.create", "payroll.bonus.apply"} <= set(actions())


def test_a_bonus_waits_for_a_day_worked(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "absent"})
    bonus = pay_rules.add_bonus(manager, site, yao, MON, 1000, "Safety award")
    bonus.refresh_from_db()
    assert bonus.applied_to is None

    roll_call(site, TUE, {yao: "present"})
    line = refreshed_line(site, TUE, yao)

    bonus.refresh_from_db()
    assert bonus.applied_to == line and line.gross == 7000


def test_a_bonus_after_payment_goes_on_the_following_pay(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})
    line = refreshed_line(site, MON, yao)
    services.record_payment(manager, line, amount=6000, proof_type="signature")
    bonus = pay_rules.add_bonus(manager, site, yao, MON, 1000, "Late bonus")
    roll_call(site, TUE, {yao: "present"})

    assert refreshed_line(site, TUE, yao).bonus_amount == 1000
    assert line_of(site, MON, yao).gross == 6000
    bonus.refresh_from_db()
    assert bonus.applied_to.period.end_date == TUE


def test_a_bonus_is_taxed_like_the_rest_of_the_gross(crew):
    site, manager, _f, yao = crew
    deduction(employee="10", employer="0")
    roll_call(site, MON, {yao: "present"})
    pay_rules.add_bonus(manager, site, yao, MON, 4000, "Overtime week")

    line = line_of(site, MON, yao)
    assert (line.gross, line.statutory_deductions, line.net) == (10000, 1000, 9000)


def test_bonus_checks_and_removal(crew):
    site, manager, _f, yao = crew
    roll_call(site, MON, {yao: "present"})
    with pytest.raises(ValidationError) as error:
        pay_rules.add_bonus(manager, site, yao, MON, 0, "")
    assert set(error.value.message_dict) == {"amount", "reason"}

    bonus = pay_rules.add_bonus(manager, site, yao, MON, 1000, "Award")
    pay_rules.delete_bonus(manager, Bonus.objects.get(pk=bonus.pk), "Wrong worker")
    assert line_of(site, MON, yao).gross == 6000

    bonus = pay_rules.add_bonus(manager, site, yao, MON, 1000, "Award")
    line = line_of(site, MON, yao)
    services.record_payment(manager, line, amount=line.net, proof_type="signature")
    with pytest.raises(Locked):
        pay_rules.delete_bonus(manager, Bonus.objects.get(pk=bonus.pk))


# --- permissions ---------------------------------------------------------------------------


def test_only_the_sites_managers_and_admins_change_the_day_extras(crew):
    site, _manager, foreman, yao = crew
    other_manager = setup_site()[1]
    accountant = UserFactory(role="accountant")
    roll_call(site, MON, {yao: "present"})
    for user in (foreman, other_manager, accountant):
        with pytest.raises(PermissionDenied):
            pay_rules.add_bonus(user, site, yao, MON, 1000, "x")
        with pytest.raises(PermissionDenied):
            pay_rules.add_piece_work(user, site, yao, MON, task(f"T{user.pk}"), Decimal("1"))
        with pytest.raises(PermissionDenied):
            pay_rules.set_stoppage(user, site, MON, "Rain")
    pay_rules.add_bonus(admin(), site, yao, MON, 1000, "By the admin")


def test_only_admins_change_the_rules(crew):
    site, manager, _f, _yao = crew
    settings = PayRuleSettings.load()
    before = pay_rules.snapshot(settings, pay_rules.RULE_FIELDS)
    settings.sunday_multiplier = Decimal("2")
    with pytest.raises(PermissionDenied):
        pay_rules.save_rules(manager, settings, before)
    with pytest.raises(PermissionDenied):
        pay_rules.save_deduction_rule(manager, DeductionRule(name="x", active_from=MON), None)
    with pytest.raises(PermissionDenied):
        pay_rules.save_site_rules(manager, SitePayRules(site=site), None)

    boss = admin()
    assert pay_rules.save_rules(boss, settings, before)
    assert not pay_rules.save_rules(
        boss, settings, pay_rules.snapshot(settings, pay_rules.RULE_FIELDS)
    )
    entry = AuditLog.objects.get(action="payroll.rules.update")
    assert (entry.before, entry.after) == ({"sunday_multiplier": "1"}, {"sunday_multiplier": "2"})


def test_an_admin_cannot_override_another_companys_site(crew):
    with use_company(CompanyFactory()):
        theirs, _m, _f = setup_site()
    with pytest.raises(PermissionDenied):
        pay_rules.save_site_rules(admin(), SitePayRules(site=theirs), None)


def test_new_rules_belong_to_the_admins_company(crew):
    boss = admin()
    rule = DeductionRule(name="Pension", employee_percent=Decimal("3"), active_from=MON)
    assert pay_rules.save_deduction_rule(boss, rule, None)
    assert rule.company_id == boss.company_id
    assert AuditLog.objects.get(action="payroll.deduction_rule.create").after["name"] == "Pension"


# --- employer cost (reports) ---------------------------------------------------------------


def test_employer_costs_per_rule(crew):
    site, _m, _f, yao = crew
    afi = hire(site, 4000)
    deduction("Pension", employee="5", employer="10", order=2)
    deduction("Health", employee="1", employer="2.5", order=1)
    roll_call(site, MON, {yao: "present", afi: "present"})
    services.cash_sheet(site, MON)
    with use_company(CompanyFactory()):
        CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
        theirs, _m2, _f2 = setup_site()
        zeb = hire(theirs, 9000)
        deduction("Pension", employee="5", employer="10")
        roll_call(theirs, MON, {zeb: "present"})
        services.cash_sheet(theirs, MON)

    cost = pay_rules.employer_costs(services.scoped_sites(admin()), MON, MON)

    assert (cost.gross, cost.employee, cost.employer) == (10000, 600, 1250)
    assert cost.total_cost == 11250
    assert [(r.name, r.employee, r.employer) for r in cost.rules] == [
        ("Health", 100, 250),
        ("Pension", 500, 1000),
    ]
    assert pay_rules.employer_costs(services.scoped_sites(admin()), TUE, TUE).employer == 0


def test_the_pay_rules_row_is_one_per_company(company):
    first = PayRuleSettings.load()
    assert PayRuleSettings.load() == first
    with use_company(CompanyFactory()) as other:
        assert PayRuleSettings.load().company_id == other.pk
    assert PayRuleSettings.load(CompanySettings.load().company_id) == first
