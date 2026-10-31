"""End to end: a phone that was offline sends its outbox late, out of order where the
contract allows it, with duplicates. The final state must be the one you get by sending
every write once, in order.

Two identical sites are run side by side: site "Once" gets each write once and in order,
site "Late" gets the messy delivery. Their final states (attendance, corrections, pay,
payments and the audit trail) are compared.

What the contract allows (docs/api/README.md, "Working offline"): writes for different
days are independent; within a day, records → submit → corrections → payments, and a
correction needs the record id from the records response. Any write may be repeated
(timeouts), and a phone that crashed may resend its whole outbox.
"""

import datetime
from collections import Counter

import pytest
from django.core.cache import cache

from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.core.models import AuditLog
from apps.payroll.models import PayLine, Payment, PayPeriod
from tests.e2e import Office, Phone, hire, lines_by_worker, make_site, new_uuid, record
from tests.factories import CompanySettingsFactory

pytestmark = pytest.mark.django_db

PIN = "5173"
MON = datetime.date(2025, 9, 1)
TUE = MON + datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


class Outbox:
    """The phone's queued writes for one site, as (label, send) pairs."""

    def __init__(self, phone, crew):
        self.phone, self.crew = phone, crew
        w = crew.workers
        site = crew.site.pk
        self.mon = {k: record(w[k], "present", "07:00", "17:00") for k in ("Ama", "Kofi", "Yao")}
        # The foreman then changes his mind about Yao (same client_uuid: same record).
        self.mon_edit = {**self.mon["Yao"], "status": "late", "arrival": "07:30"}
        self.tue = {
            "Ama": record(w["Ama"], "present"),
            "Kofi": record(w["Kofi"], "half_day"),
            "Yao": record(w["Yao"], "absent"),
        }
        self.submit_mon, self.submit_tue = new_uuid(), new_uuid()
        self.correction = new_uuid()
        self.record_ids = {}
        self.attendance = {
            "mon-records": lambda: phone.put_records(site, MON, list(self.mon.values())),
            "mon-edit": lambda: phone.put_records(site, MON, [self.mon_edit]),
            "mon-submit": lambda: phone.submit(site, MON, self.submit_mon, "Rain at noon"),
            "tue-records": lambda: phone.put_records(site, TUE, list(self.tue.values())),
            "tue-submit": lambda: phone.submit(site, TUE, self.submit_tue),
            "tue-correction": lambda: phone.correct(
                self.record_ids[("tue", "Kofi")],
                self.correction,
                "note",
                "Left at noon for the clinic",
                "Forgot the note",
            ),
        }
        self.payment_uuids = {}

    def send(self, label, expect=(200, 201)):
        response = self.attendance[label]()
        assert response.status_code in expect, (label, response.status_code, response.content)
        if label.endswith("records") and response.status_code == 200:
            day = "mon" if label.startswith("mon") else "tue"
            for item in response.json()["records"]:
                key = next(k for k, w in self.crew.workers.items() if w.pk == item["worker_id"])
                self.record_ids[(day, key)] = item["id"]
        return response

    def payments(self, day):
        """The payment writes for a day: made when the foreman hands over the cash."""
        payout = self.phone.payout(self.crew.site.pk, day).json()
        assert payout["status"] == "open", payout
        writes = []
        for key, worker in sorted(self.crew.workers.items()):
            line = lines_by_worker(payout).get(worker.pk)
            if line is None:
                continue
            client_uuid = self.payment_uuids.setdefault((day, key), new_uuid())
            paid_at = f"{day.isoformat()}T16:{len(writes):02d}:00Z"  # the phone's clock
            writes.append((key, line["pay_line_id"], line["net"], client_uuid, paid_at))
        return writes

    def pay(self, write, expect=(200, 201)):
        _key, line_id, net, client_uuid, paid_at = write
        response = self.phone.pay(line_id, net, client_uuid, paid_at=paid_at)
        assert response.status_code in expect, (write, response.content)
        return response


def _crew(name, phone_number):
    crew = make_site(name, phone_number=phone_number, pin=PIN)
    hire(crew, "Ama", 6000)
    hire(crew, "Kofi", 5000)
    hire(crew, "Yao", 4000, pay_type="daily")
    return crew


def _approve(office, crew, day):
    office.approve(AttendanceDay.objects.get(site=crew.site, date=day).pk)


def _snapshot(crew) -> dict:
    """Everything the flow wrote for a site, keyed by worker name instead of ids."""
    name = {w.pk: k for k, w in crew.workers.items()}
    site = crew.site
    return {
        "days": sorted(
            AttendanceDay.objects.filter(site=site).values_list("date", "status", "foreman_note")
        ),
        "records": sorted(
            (r.day.date, name[r.worker_id], r.status, r.arrival, r.departure, r.note, r.corrected)
            for r in AttendanceRecord.objects.filter(day__site=site).select_related("day")
        ),
        "corrections": sorted(
            (name[c.record.worker_id], c.field, c.old_value, c.new_value, c.reason)
            for c in AttendanceCorrection.objects.filter(record__day__site=site)
        ),
        "periods": sorted(
            PayPeriod.objects.filter(site=site).values_list("start_date", "end_date", "status")
        ),
        "lines": sorted(
            (line.period.end_date, name[line.worker_id], line.gross, line.net)
            for line in PayLine.objects.filter(period__site=site).select_related("period")
        ),
        "payments": sorted(
            (p.pay_line.period.end_date, name[p.pay_line.worker_id], p.amount, p.paid_at)
            for p in Payment.objects.filter(pay_line__period__site=site).select_related(
                "pay_line__period"
            )
        ),
    }


def _audit_counts() -> Counter:
    return Counter(AuditLog.objects.values_list("action", flat=True))


def test_late_out_of_order_duplicated_outbox_ends_like_sending_once():
    CompanySettingsFactory()
    once, late = _crew("Once", "+228 90 00 00 01"), _crew("Late", "+228 90 00 00 02")

    # --- site "Once": every write once, in order ------------------------------------------
    before = _audit_counts()
    box = Outbox(Phone(once.phone_number, PIN), once)
    for label in ("mon-records", "mon-edit", "mon-submit", "tue-records", "tue-submit"):
        box.send(label)
    box.send("tue-correction")
    office = Office(once.manager)
    _approve(office, once, MON)
    _approve(office, once, TUE)
    for day in (MON, TUE):
        for write in box.payments(day):
            box.pay(write)
    once_audit = _audit_counts() - before

    # --- site "Late": Tuesday first, every write twice, then the whole outbox again -------
    before = _audit_counts()
    box = Outbox(Phone(late.phone_number, PIN), late)
    for label in ("tue-records", "tue-submit", "tue-correction"):
        box.send(label)
        box.send(label)  # timeout: sent again
    for label in ("mon-records", "mon-edit", "mon-submit"):
        box.send(label)
        box.send(label)
    # Crash before the outbox was cleared: everything again, in order. A batch that would
    # now change something hits a submitted day and is refused (423) instead of undoing
    # later writes: the stale first Monday batch (Yao's edit came after it) and Tuesday's
    # batch (Kofi's note was corrected since). The server wins; nothing changes.
    stale = {"mon-records", "tue-records"}
    for label in ("mon-records", "mon-edit", "mon-submit", "tue-records", "tue-submit"):
        box.send(label, expect=(423,) if label in stale else (200,))
    box.send("tue-correction")
    office = Office(late.manager)
    _approve(office, late, TUE)
    _approve(office, late, MON)
    # Payments: reached the server hours later, Tuesday's first, in reverse, twice each.
    writes = [*box.payments(TUE)[::-1], *box.payments(MON)[::-1]]
    for write in writes:
        assert box.pay(write).status_code == 201
        replay = box.pay(write)
        assert replay.status_code == 200
        assert replay["Idempotent-Replayed"] == "true"
    for write in writes:  # and the whole payment outbox once more
        assert box.pay(write).status_code == 200
    late_audit = _audit_counts() - before

    # --- same final state, same audit trail -----------------------------------------------
    assert _snapshot(late) == _snapshot(once)
    assert late_audit == once_audit
    assert _snapshot(once)["payments"][0][3] == datetime.datetime(
        2025, 9, 1, 16, 0, tzinfo=datetime.UTC
    )  # paid_at is the phone's time, not the time it arrived
    for crew in (once, late):
        assert {s for _d, s, _n in _snapshot(crew)["days"]} == {"approved"}
        assert {p[2] for p in _snapshot(crew)["periods"]} == {"paid"}
