"""Fill an empty database with a realistic demo: three sites, their people and two weeks
of roll calls, approvals, pay, cash and advances.

Everything goes through the real services (attendance, payroll, workers, accounts), so the
amounts, cash sheets and audit log are exactly what the app would produce. Random choices
use a fixed seed: the same command on the same day gives the same data.

The demo is one company (``--company``, default "Polaris Demo Construction"). ``--keep``
adds another company to a database that already has one, to show several companies: its
usernames start with the company's short name (``other-co-admin``) and its foremen get
other phone numbers (phones are unique across companies). The first run also creates a
platform admin, ``platform``, who can switch between the companies.

    DJANGO_DEBUG=true uv run python manage.py seed_demo            # empty database only
    DJANGO_DEBUG=true uv run python manage.py seed_demo --flush    # wipe everything first
    DJANGO_DEBUG=true uv run python manage.py seed_demo --keep --company "Other Co"
"""

import datetime
import random
import uuid
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts import services as account_services
from apps.accounts.models import User
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceRecord
from apps.core import dates
from apps.core import services as core_services
from apps.core.choices import PayType
from apps.core.models import Company, CompanySettings
from apps.core.tenancy import use_company
from apps.payroll import services as payroll
from apps.payroll.models import PayLine
from apps.workers import services as worker_services
from apps.workers.models import Trade, Worker
from apps.worksites.models import Site

from . import _demo_wave4 as wave4

Status = AttendanceRecord.Status

DEMO_PASSWORD = "Polaris-demo-2026"
DEMO_COMPANY = "Polaris Demo Construction"
PLATFORM_ADMIN = "platform"

TRADES = {  # trade → daily rate in XOF (the electrician is paid by the hour)
    "Mason": 6000,
    "Carpenter": 5500,
    "Labourer": 3500,
    "Steel fixer": 5000,
    "Painter": 4500,
    "Electrician": 900,
    "Welder": 5500,
}

FIRST_NAMES = [
    "Yao", "Komlan", "Afi", "Kodjo", "Akouvi", "Sena", "Delali", "Selom", "Kafui", "Edo",
    "Elikem", "Dela", "Yawa", "Kossiwa", "Mawuena", "Fafa", "Kwami", "Adjo", "Kokou",
    "Abla", "Senyo", "Dzifa", "Komi", "Ayele", "Enyonam", "Atsu", "Mensah", "Esi",
    "Kwasi", "Amivi", "Folly", "Nana", "Kekeli", "Elom", "Agbeko", "Afiwa",
]  # fmt: skip
LAST_NAMES = [
    "Mensah", "Agbeko", "Dosseh", "Atsu", "Tete", "Kumah", "Agbo", "Akakpo", "Amedome",
    "Gbedemah", "Dzidzor", "Kumassi", "Sossou", "Adzo", "Lawson", "Fiagbe", "Kpodar",
    "Ahadji", "Amegah", "Adjovi", "Togbe", "Kuadjo", "Ayivi", "Glover",
]  # fmt: skip

SITES = [
    {
        "key": "riverside",
        "name": "Riverside Apartments",
        "address": "Block B, Riverside road",
        "manager": ("edem", "Edem", "Kpodar"),
        "foreman": ("kossi", "Kossi", "Amegah", "+22890112233", "4821"),
        "crew": ["Mason"] * 4 + ["Carpenter"] * 2 + ["Labourer"] * 4 + ["Steel fixer", "Painter"],
        "budget": 4_000_000,
    },
    {
        "key": "market",
        "name": "Market Hall Renovation",
        "address": "Central market, east wing",
        "manager": ("ama", "Ama", "Fiagbe"),
        "foreman": ("mawuli", "Mawuli", "Adjovi", "+22891700355", "5937"),
        "crew": ["Mason"] * 3 + ["Labourer"] * 4 + ["Electrician", "Painter", "Carpenter"],
        "budget": None,
    },
    {
        "key": "bridge",
        "name": "Route 4 Bridge",
        "address": "Route 4, km 12",
        "manager": ("edem", "Edem", "Kpodar"),
        "foreman": ("sena", "Sena", "Lawson", "+22898115230", "7264"),
        "crew": ["Welder"] * 2 + ["Steel fixer"] * 2 + ["Labourer"] * 4,
        "budget": 3_000_000,
        "weekly": True,
    },
]


class Command(BaseCommand):
    help = "Fill the database with demo data (sites, people, two weeks of attendance and pay)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush", action="store_true", help="Delete ALL data in the database first."
        )
        parser.add_argument(
            "--days", type=int, default=14, help="Calendar days of history (default 14)."
        )
        parser.add_argument("--password", default=DEMO_PASSWORD, help="Password for all users.")
        parser.add_argument(
            "--company", default=DEMO_COMPANY, help=f'Company name (default "{DEMO_COMPANY}").'
        )
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Add this company to a database that already has data (another company).",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            call_command("flush", interactive=False, verbosity=0)
        elif not options["keep"] and (User.objects.exists() or Site.objects.exists()):
            raise CommandError(
                "The database already has data. Run with --flush to wipe it and seed again, "
                'or with --keep --company "Other Co" to add another company.'
            )
        name = " ".join(options["company"].split())
        if not name:
            raise CommandError("The company needs a name.")
        if Company.objects.filter(name__iexact=name).exists():
            raise CommandError(f'A company named "{name}" already exists.')
        # The first demo company keeps the short usernames; later ones get a prefix.
        self.prefix = f"{slugify(name)}-" if User.objects.exists() else ""
        self.rng = random.Random(42)
        self.password = options["password"]
        self.warnings: list[str] = []
        self._users: dict[str, User] = {}

        with transaction.atomic():
            self.company_row = core_services.create_company(
                name, currency="XOF", currency_decimals=0
            )
            self.platform_admin = self._platform_admin()
        with use_company(self.company_row):
            self._seed(options["days"])

    def _seed(self, days: int):
        self.company = CompanySettings.load()
        self.today = dates.company_today(self.company)
        self.first_day = self.today - datetime.timedelta(days=days)
        # Start on a Sunday so weekly pay periods (Sunday to Saturday) are complete.
        while self.first_day.isoweekday() != 7:
            self.first_day -= datetime.timedelta(days=1)

        # Wave 4 (pay rules, payouts, check-in, portal, messaging): the first company only
        # (portal phone numbers are unique across companies).
        self.wave4 = not self.prefix
        self.demo_holiday = None
        with transaction.atomic():
            self.admin, self.accountant = self._office_users()
            self.sites = self._sites()
            self.crews = self._crews()
            self._advances()
            if self.wave4:
                wave4.rules(self)
        self._history()
        self._today()
        if self.wave4:
            wave4.after(self)
        self._report()

    # --- setup -------------------------------------------------------------------------

    def _platform_admin(self) -> User | None:
        """A superuser without a company (switches between companies), made once."""
        if User.objects.filter(username=PLATFORM_ADMIN).exists():
            return None
        user = User(
            username=PLATFORM_ADMIN,
            first_name="Platform",
            last_name="Admin",
            role=User.Role.ADMIN,
            email="platform@example.com",
            is_staff=True,
            is_superuser=True,
            company=None,
        )
        user.set_password(self.password)
        user.save()
        return user

    def _user(self, username, first, last, role, **extra) -> User:
        """A user of the demo company (the same person when a name comes twice)."""
        username = self.prefix + username
        if username in self._users:
            return self._users[username]
        if User.objects.filter(username=username).exists():
            raise CommandError(f'The username "{username}" is taken: pick another --company.')
        user = User(username=username, first_name=first, last_name=last, role=role, **extra)
        user.set_password(self.password)
        user.save()
        self._users[username] = user
        return user

    def _phone(self, phone: str, index: int) -> str:
        """Foreman phones are unique across companies: other demo companies get new ones."""
        if not self.prefix:
            return phone
        return f"+2299{self.company_row.pk % 1000:03d}{index:04d}"

    def _office_users(self):
        admin = self._user(
            "admin",
            "Abel",
            "Admin",
            User.Role.ADMIN,
            email="admin@example.com",
        )
        accountant = self._user("afua", "Afua", "Mensah", User.Role.ACCOUNTANT)
        return admin, accountant

    def _sites(self) -> dict[str, dict]:
        sites = {}
        for index, spec in enumerate(SITES, start=1):
            m_user, m_first, m_last = spec["manager"]
            manager = self._user(m_user, m_first, m_last, User.Role.SITE_MANAGER)
            f_user, f_first, f_last, phone, pin = spec["foreman"]
            phone = self._phone(phone, index)
            foreman = self._user(f_user, f_first, f_last, User.Role.FOREMAN, phone=phone)
            account_services.set_pin(foreman, pin, actor=self.admin)
            site = Site.objects.create(
                name=spec["name"],
                address=spec["address"],
                started_on=self.first_day - datetime.timedelta(days=60),
                manager=manager,
                labour_budget=spec["budget"],
                pay_frequency="weekly" if spec.get("weekly") else None,
            )
            site.foremen.add(foreman)
            sites[spec["key"]] = {
                **spec,
                "site": site,
                "manager": manager,
                "foreman": foreman,
                "foreman_login": (f_user, f_first, f_last, phone, pin),
            }
        return sites

    def _crews(self) -> dict[str, list[Worker]]:
        trades = {
            name: Trade.objects.get_or_create(company=self.company_row, name=name)[0]
            for name in TRADES
        }
        names = iter(
            self.rng.sample(
                [(f, last) for f in FIRST_NAMES for last in LAST_NAMES],
                k=sum(len(spec["crew"]) for spec in SITES),
            )
        )
        start = self.first_day - datetime.timedelta(days=30)
        crews = {}
        for key, info in self.sites.items():
            crews[key] = []
            for trade in info["crew"]:
                first, last = next(names)
                hourly = trade == "Electrician"
                worker = worker_services.create_worker(
                    actor=self.admin,
                    site=info["site"],
                    start_date=start,
                    pay_type=PayType.HOURLY if hourly else PayType.DAILY,
                    amount=TRADES[trade],
                    effective_from=start,
                    first_name=first,
                    last_name=last,
                    phone=f"+228 9{self.rng.randint(0, 9)} {self.rng.randint(10, 99)} "
                    f"{self.rng.randint(10, 99)} {self.rng.randint(10, 99)}",
                    trade=trades[trade],
                )
                crews[key].append(worker)
        return crews

    def _advances(self):
        # Given a few days ago, so part of each advance is still owed in the demo.
        given_on = self.today - datetime.timedelta(days=6)
        for key, index, amount in (("riverside", 0, 15_000), ("market", 3, 8_000)):
            info = self.sites[key]
            payroll.create_advance(
                info["manager"],
                self.crews[key][index],
                amount=amount,
                deduction_per_pay=2_000,
                reason="Rent" if index == 0 else "Medical",
                given_on=given_on,
                proof_type="signature",
            )

    # --- two weeks of history -------------------------------------------------------------

    def _working_days(self):
        day = self.first_day
        while day < self.today:
            if self.company.is_working_day(day) or day == self.demo_holiday:
                yield day
            day += datetime.timedelta(days=1)

    def _history(self):
        yesterday = self.today - datetime.timedelta(days=1)
        for day in self._working_days():
            for key, info in self.sites.items():
                if day == self.demo_holiday and key != "bridge":
                    continue  # a public holiday: only the bridge crew worked (paid × 2)
                awaiting = key == "riverside" and day == yesterday
                self._roll_call(info, self.crews[key], day, approve=not awaiting)
                if self.wave4:
                    wave4.extras_for_day(self, key, info, day)
                if awaiting:
                    continue
                self._pay_day(info, day, partial=key == "market" and day == yesterday)
                if key == "market" and day == self.today - datetime.timedelta(days=3):
                    self._correct_paid_day(info, day)

    def _status_for(self, worker: Worker, key: str, day: datetime.date) -> str:
        # Kodjo-style absence streak at the market: the 4th worker misses the last 3 days.
        if key == "market" and worker == self.crews[key][4]:
            if (self.today - day).days <= 3:
                return Status.ABSENT
        roll = self.rng.random()
        if roll < 0.80:
            return Status.PRESENT
        if roll < 0.88:
            return Status.LATE
        if roll < 0.94:
            return Status.HALF_DAY
        return Status.ABSENT

    def _record(self, worker: Worker, status: str) -> dict:
        arrival = departure = None
        if status == Status.PRESENT:
            arrival = datetime.time(6, self.rng.randint(45, 59))
            departure = datetime.time(17, self.rng.choice([0, 0, 0, 15, 30]))
        elif status == Status.LATE:
            arrival = datetime.time(7, self.rng.randint(25, 55))
            departure = datetime.time(17, 0)
        elif status == Status.HALF_DAY:
            arrival, departure = datetime.time(7, 0), datetime.time(12, 0)
        return {
            "client_uuid": uuid.uuid4(),
            "worker_id": worker.pk,
            "status": status,
            "arrival": arrival,
            "departure": departure,
            "note": "",
        }

    def _at(self, day: datetime.date, hour: int, minute: int) -> datetime.datetime:
        zone = ZoneInfo(self.company.time_zone or "UTC")
        return timezone.make_aware(
            datetime.datetime.combine(day, datetime.time(hour, minute)), zone
        )

    def _roll_call(self, info, crew, day, *, approve=True, submit=True):
        foreman, key = info["foreman"], info["key"]
        attendance_day = attendance.get_or_create_day(info["site"], day, foreman)
        records = [self._record(w, self._status_for(w, key, day)) for w in crew]
        attendance.upsert_records(attendance_day, records, foreman)
        if not submit:
            return attendance_day
        attendance.submit_day(
            attendance_day,
            foreman,
            uuid.uuid4(),
            note="",
            client_submitted_at=self._at(day, 7, self.rng.randint(35, 55)),
        )
        if approve:
            attendance.approve_day(attendance_day, info["manager"])
        return attendance_day

    def _pay_day(self, info, day, *, partial=False):
        site, manager, foreman = info["site"], info["manager"], info["foreman"]
        periods = payroll.sync_sheet(site, day)
        if not periods:
            return  # not a pay day for this site (weekly pay)
        lines = list(
            PayLine.objects.filter(period__in=periods, net__gt=0).order_by("worker__last_name")
        )
        if not lines:
            return
        total = sum(line.net for line in lines)
        payroll.issue_cash(
            manager, site, day, -(-total // 5_000) * 5_000, received_by=foreman, note="Demo"
        )
        to_pay = lines[: len(lines) // 2] if partial else lines
        for index, line in enumerate(to_pay):
            try:
                payroll.record_payment(
                    foreman,
                    line,
                    amount=line.net,
                    proof_type="signature" if index % 3 else "thumb_mark",
                    paid_at=self._at(day, 17, 30 + index % 25),
                    client_uuid=uuid.uuid4(),
                )
            except Exception as exc:  # noqa: BLE001 — keep seeding, report at the end
                self.warnings.append(f"{site.name} {day}: payment skipped ({exc})")
        if not partial and day <= self.today - datetime.timedelta(days=4):
            try:
                payroll.lock_sheet(manager, site, day)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"{site.name} {day}: not locked ({exc})")

    def _correct_paid_day(self, info, day):
        """A paid day corrected afterwards: the overpayment is recovered on the next pay."""
        record = (
            AttendanceRecord.objects.filter(day__site=info["site"], day__date=day)
            .exclude(status=Status.ABSENT)
            .order_by("worker__last_name")
            .first()
        )
        if record is None:
            return
        try:
            attendance.correct_record(
                record,
                "status",
                Status.ABSENT,
                "Left the site at 08:00; the gate log shows no return.",
                info["manager"],
                uuid.uuid4(),
            )
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"Correction on {day} skipped ({exc})")

    # --- today ---------------------------------------------------------------------------

    def _today(self):
        if not self.company.is_working_day(self.today):
            return
        # Riverside and the market submitted this morning; the bridge hasn't started.
        for key in ("riverside", "market"):
            info = self.sites[key]
            day = self._roll_call(info, self.crews[key], self.today, approve=False)
            if key == "riverside":
                record = day.records.filter(status=Status.LATE).first() or day.records.first()
                try:
                    attendance.correct_record(
                        record,
                        "status",
                        Status.PRESENT,
                        "Bus breakdown, called ahead.",
                        info["foreman"],
                        uuid.uuid4(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"Today's correction skipped ({exc})")

    # --- report --------------------------------------------------------------------------

    def _report(self):
        out = self.stdout
        out.write(self.style.SUCCESS("Demo data created."))
        workers = Worker.objects.filter(company=self.company_row).count()
        out.write(f"  Company: {self.company.company_name} · XOF · today = {self.today}")
        out.write(f"  Workers: {workers} on {len(self.sites)} sites")
        out.write(f"  Office sign-in (password for everyone: {self.password}):")
        p = self.prefix
        out.write(f"    {p}admin  — admin (all sites, settings, users)")
        out.write(f"    {p}edem   — site manager (Riverside Apartments, Route 4 Bridge)")
        out.write(f"    {p}ama    — site manager (Market Hall Renovation)")
        out.write(f"    {p}afua   — accountant")
        if self.platform_admin is not None:
            out.write(f"    {PLATFORM_ADMIN} — platform admin (switches between companies)")
        out.write("  Foreman phone app (phone → PIN):")
        for info in self.sites.values():
            _, first, last, phone, pin = info["foreman_login"]
            out.write(f"    {first} {last}: {phone} → {pin} ({info['name']})")
        if self.wave4:
            self._report_wave4()
        for warning in self.warnings:
            out.write(self.style.WARNING(f"  Note: {warning}"))

    def _report_wave4(self):
        out = self.stdout
        out.write("  Worker portal (/portal/, phone → PIN):")
        for worker, phone, pin in getattr(self, "demo_portal", []):
            out.write(f"    {worker.full_name}: {phone} → {pin}")
        kiosk = getattr(self, "demo_kiosk", None)
        if kiosk is not None:
            bridge = self.sites["bridge"]
            _, first, last, phone, pin = bridge["foreman_login"]
            out.write(
                f"  Kiosk phone: {first} {last} ({phone} → {pin}) signing in with device id "
                f"{wave4.KIOSK_DEVICE_ID} is the {bridge['name']} kiosk (check-ins only)."
            )
        batches = ", ".join(
            f"{b.reference} ({b.get_status_display().lower()})"
            for b in getattr(self, "demo_batches", [])
        )
        if batches:
            out.write(f"  Transfer batches (Market Hall): {batches}")
        out.write(
            "  Pay rules: Social security 4 % / 16 %, Sunday × 1.5, public holiday × 2, "
            "piece work and a bonus two days ago; messaging on (log provider)."
        )
