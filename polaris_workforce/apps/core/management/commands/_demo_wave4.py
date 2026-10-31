"""Wave 4 demo data for ``seed_demo`` (the first demo company only): pay rules, payouts,
check-in, worker portal and messaging, so every Wave 4 screen has something to show.

``rules(cmd)`` runs before the two weeks of history (the pay of those days uses the
rules); ``extras_for_day(cmd, key, info, day)`` runs for each roll call before its pay
day; ``after(cmd)`` runs once the history and today are in. Everything goes through the
apps' services, so the audit log and the amounts are the real ones.
"""

import datetime
import uuid
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import ForemanDevice
from apps.checkin import services as checkin
from apps.messaging import services as messaging
from apps.messaging.models import MessagingSettings
from apps.messaging.providers import LogProvider
from apps.payouts import services as payouts
from apps.payouts.models import PayoutAccount
from apps.payroll import pay_rules
from apps.payroll.models import DeductionRule, PayLine, Payment, PayRuleSettings, TaskType
from apps.portal import services as portal
from apps.workers import services as worker_services

KIOSK_DEVICE_ID = "demo-kiosk-bridge"
PORTAL_SIGN_IN = "http://127.0.0.1:8000/portal/sign-in/"
PORTAL_PINS = ("4071", "5283", "6194")  # fixed, so the demo can be written down
# Around Lomé (WGS 84): one point per site, a 150 m circle each.
GEOFENCES = {
    "riverside": (Decimal("6.137810"), Decimal("1.212560")),
    "market": (Decimal("6.130440"), Decimal("1.222780")),
    "bridge": (Decimal("6.171230"), Decimal("1.183400")),
}
MOMO = "mobile_money"
BANK = "bank_transfer"


def _past_working_day(cmd, at_least_days_ago: int, exclude=()) -> datetime.date | None:
    day = cmd.today - datetime.timedelta(days=at_least_days_ago)
    while day > cmd.first_day:
        if cmd.company.is_working_day(day) and day not in exclude:
            return day
        day -= datetime.timedelta(days=1)
    return None


# --- before the history: rules ------------------------------------------------------------


def rules(cmd) -> None:
    admin = cmd.admin
    settings = PayRuleSettings.load(cmd.company_row)
    before = pay_rules.snapshot(settings, pay_rules.RULE_FIELDS)
    settings.sunday_multiplier = Decimal("1.5")
    settings.holiday_multiplier = Decimal("2")
    settings.full_clean()
    pay_rules.save_rules(admin, settings, before)

    rule = DeductionRule(
        name="Social security",
        employee_percent=Decimal("4"),
        employer_percent=Decimal("16"),
        base_ceiling=25_000,
        active_from=cmd.first_day - datetime.timedelta(days=60),
        order=1,
    )
    rule.company_id = cmd.company_row.pk
    rule.full_clean()
    pay_rules.save_deduction_rule(admin, rule, None)

    cmd.demo_tasks = {}
    for name, unit, rate in (("Block laying", "block", 50), ("Plastering", "m²", 400)):
        task = TaskType(name=name, unit=unit, unit_rate=rate)
        task.company_id = cmd.company_row.pk
        task.full_clean()
        pay_rules.save_task_type(admin, task, None)
        cmd.demo_tasks[name] = task

    # A public holiday the bridge crew worked (paid × 2 on the weekly pay).
    cmd.demo_holiday = _past_working_day(cmd, 5)
    if cmd.demo_holiday is not None:
        cmd.company.public_holidays = [
            *(cmd.company.public_holidays or []),
            {"date": cmd.demo_holiday.isoformat(), "name": "Demo public holiday"},
        ]
        cmd.company.save()
    # A rain day on the bridge: the crew present gets the stoppage share of the day.
    cmd.demo_stoppage = _past_working_day(cmd, 8, exclude={cmd.demo_holiday})
    if cmd.demo_stoppage is not None:
        pay_rules.set_stoppage(
            admin, cmd.sites["bridge"]["site"], cmd.demo_stoppage, "Heavy rain, site flooded"
        )


def extras_for_day(cmd, key: str, info: dict, day: datetime.date) -> None:
    """Piece work and a bonus two days ago, before that day is paid."""
    if day != cmd.today - datetime.timedelta(days=2):
        return
    manager, site = info["manager"], info["site"]
    if key == "riverside":
        masons = cmd.crews[key][:2]
        for worker, quantity in zip(masons, (Decimal("120"), Decimal("85")), strict=True):
            try:
                pay_rules.add_piece_work(
                    manager, site, worker, day, cmd.demo_tasks["Block laying"], quantity, "Wall C"
                )
            except Exception as exc:  # noqa: BLE001 — absent that day, for instance
                cmd.warnings.append(f"Piece work for {worker} skipped ({exc})")
    if key == "market":
        worker = cmd.crews[key][0]
        try:
            pay_rules.add_bonus(manager, site, worker, day, 2_500, "Roof finished early")
        except Exception as exc:  # noqa: BLE001
            cmd.warnings.append(f"Bonus for {worker} skipped ({exc})")


# --- after the history ----------------------------------------------------------------------


def after(cmd) -> None:
    cmd.demo_portal = []
    cmd.demo_batches = []
    _payouts(cmd)
    _checkin(cmd)
    _messaging_and_portal(cmd)


def _account(cmd, worker, method, index):
    if method == MOMO:
        account = PayoutAccount(
            worker=worker,
            method=MOMO,
            mobile_operator="Flooz" if index % 2 else "T-Money",
            mobile_number=f"+228 9{index % 10} 4{index % 10} 12 {30 + index:02d}",
        )
    else:
        account = PayoutAccount(
            worker=worker,
            method=BANK,
            bank_name="Ecobank Togo",
            bank_code="TG055",
            account_number=f"0055010{index:04d}12345",
        )
    payouts.save_account(cmd.admin, account)
    worker_services.update_worker(worker, {"payment_method": method}, actor=cmd.admin)


def _payouts(cmd) -> None:
    market = cmd.sites["market"]["site"]
    unpaid = (
        PayLine.objects.filter(period__site=market, net__gt=0, payments__isnull=True)
        .exclude(period__status="locked")
        .select_related("worker")
        .order_by("-period__end_date", "worker__last_name")
    )
    latest = unpaid.first()
    if latest is None:
        cmd.warnings.append("No unpaid Market Hall pay left: no transfer batches.")
        return
    pay_date = latest.period.end_date
    workers = [line.worker for line in unpaid.filter(period__end_date=pay_date)]
    momo, bank = workers[:3], workers[3:5]
    for index, worker in enumerate(momo):
        _account(cmd, worker, MOMO, index)
    for index, worker in enumerate(bank):
        _account(cmd, worker, BANK, index + 10)
    # Accounts of a few more workers (already paid in cash this time).
    others = [w for w in cmd.crews["market"] if w not in workers][:2]
    for index, worker in enumerate(others):
        _account(cmd, worker, MOMO, index + 20)
    _account(cmd, cmd.crews["riverside"][3], BANK, 30)

    if momo:
        batch = payouts.create_batch(cmd.admin, pay_date, MOMO, market)
        payouts.export_batch(cmd.admin, batch)
        for number, line in enumerate(batch.lines.all(), 1):
            payouts.confirm_line(cmd.admin, line, f"MP{pay_date:%y%m%d}.{number:04d}")
        batch.refresh_from_db()
        cmd.demo_batches.append(batch)
    if bank:
        batch = payouts.create_batch(cmd.admin, pay_date, BANK, market)
        payouts.export_batch(cmd.admin, batch)
        batch.refresh_from_db()
        cmd.demo_batches.append(batch)


def _checkin(cmd) -> None:
    all_workers = [w for crew in cmd.crews.values() for w in crew]
    checkin.issue_missing_badges(all_workers, cmd.admin)
    for key, (latitude, longitude) in GEOFENCES.items():
        checkin.set_geofence(
            cmd.sites[key]["site"],
            latitude=latitude,
            longitude=longitude,
            radius_m=150,
            actor=cmd.admin,
        )
    bridge = cmd.sites["bridge"]
    device, _created = ForemanDevice.objects.get_or_create(
        user=bridge["foreman"],
        device_id=KIOSK_DEVICE_ID,
        defaults={
            "name": "Bridge gate kiosk",
            "token_hash": ForemanDevice.hash_token(uuid.uuid4().hex),
        },
    )
    checkin.enable_kiosk(device, bridge["site"], cmd.admin)
    cmd.demo_kiosk = device

    if not cmd.company.is_working_day(cmd.today):
        return
    now = timezone.now()
    start = cmd._at(cmd.today, 0, 1)
    scans = [
        ("riverside", None, 4),
        ("market", None, 4),
        ("bridge", device, 3),
    ]
    for key, kiosk_device, count in scans:
        info = cmd.sites[key]
        latitude, longitude = GEOFENCES[key]
        for index, worker in enumerate(cmd.crews[key][:count]):
            badge = checkin.active_badge(worker)
            at = max(now - datetime.timedelta(minutes=40 - 4 * index), start)
            outside = key == "market" and index == count - 1  # one scan from the road
            try:
                checkin.record_checkin(
                    user=info["foreman"],
                    device=kiosk_device,
                    site=info["site"],
                    direction="in",
                    at=at,
                    client_uuid=uuid.uuid4(),
                    badge=badge.payload,
                    latitude=latitude + (Decimal("0.004000") if outside else Decimal("0")),
                    longitude=longitude,
                    accuracy_m=12,
                )
            except Exception as exc:  # noqa: BLE001
                cmd.warnings.append(f"Check-in of {worker} skipped ({exc})")


def _messaging_and_portal(cmd) -> None:
    settings = MessagingSettings.load(cmd.company_row)
    settings.enabled = True
    settings.default_country_code = "228"
    settings.full_clean()
    settings.save()

    for key, demo_pin in zip(("market", "riverside", "bridge"), PORTAL_PINS, strict=True):
        worker = cmd.crews[key][0]
        try:
            access, pin = portal.give_access(worker, actor=cmd.admin, pin=demo_pin)
        except portal.AccessError as exc:
            cmd.warnings.append(f"Portal access for {worker} skipped ({exc})")
            continue
        portal.text_pin(worker, pin, PORTAL_SIGN_IN)
        cmd.demo_portal.append((worker, access.phone, pin))

    recent = Payment.objects.filter(pay_line__period__site__company=cmd.company_row).order_by(
        "-paid_at", "-pk"
    )[:3]
    for payment in recent:
        messaging.queue_payslip(payment)
    messaging.send_pending(cmd.company_row, provider=LogProvider())
