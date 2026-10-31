"""The contract's examples (apps/api/examples.py) are valid and tell one coherent story."""

import datetime

import pytest

from apps.accounts.api.serializers import MeSerializer, PinLoginResponseSerializer
from apps.api import examples as ex
from apps.attendance.api.serializers import (
    AttendanceDayListSerializer,
    AttendanceDaySerializer,
    CorrectionSerializer,
    SyncSerializer,
)
from apps.payroll.api.serializers import PaymentSerializer, PayoutSerializer
from apps.worksites.api.serializers import CrewSerializer


def _ts(value):
    return datetime.datetime.fromisoformat(value)


@pytest.mark.parametrize(
    ("serializer", "data"),
    [
        (MeSerializer, {**ex.USER, "company": ex.COMPANY, "sites": [ex.SITE]}),
        (
            PinLoginResponseSerializer,
            {"token": ex.TOKEN, "device": ex.DEVICE, "user": ex.USER, "sites": [ex.SITE]},
        ),
        (CrewSerializer, ex.CREW),
        (AttendanceDaySerializer, {**ex.DRAFT_DAY, "records": ex.RECORDS}),
        (AttendanceDaySerializer, {**ex.SUBMITTED_DAY, "records": ex.RECORDS}),
        (AttendanceDaySerializer, {**ex.NOT_STARTED_DAY, "records": []}),
        (AttendanceDayListSerializer, {"site_id": 3, "days": [ex.SUBMITTED_DAY, ex.PREVIOUS_DAY]}),
        (CorrectionSerializer, ex.CORRECTION),
        (
            SyncSerializer,
            {
                "server_time": ex.SERVER_TIME,
                "since": ex.SYNCED_SINCE,
                "days": [ex.APPROVED_DAY],
                "records": [ex.CORRECTED_RECORD],
                "corrections": [ex.CORRECTION],
            },
        ),
        (PayoutSerializer, ex.PAYOUT),
        (PaymentSerializer, ex.PAYMENT),
    ],
)
def test_examples_match_their_serializers(serializer, data):
    checked = serializer(data=data)

    assert checked.is_valid(), checked.errors


def test_one_crew_everywhere():
    crew = {w["id"]: w for w in ex.CREW["workers"]}
    assert ex.CREW["count"] == len(crew) == ex.COUNTS["expected"]
    assert {r["worker_id"] for r in ex.RECORDS} == set(crew)
    for line in ex.PAYOUT["lines"]:
        worker = crew[line["worker"]["id"]]
        assert line["worker"]["name"] == f"{worker['first_name']} {worker['last_name']}"
        assert line["worker"]["photo_url"] == worker["photo_url"]
    # Kodjo was absent: no pay line, and his absent streak is consistent with day 209.
    absent = {r["worker_id"] for r in ex.RECORDS if r["status"] == "absent"}
    assert absent == set(crew) - {line["worker"]["id"] for line in ex.PAYOUT["lines"]}
    assert ex.PREVIOUS_DAY["counts"]["absent"] == 1
    # Counts agree with the records.
    for status in ("present", "absent", "half_day", "late"):
        assert ex.COUNTS[status] == sum(r["status"] == status for r in ex.RECORDS)


def test_records_are_placed_on_their_day():
    for record in [*ex.RECORDS, ex.CORRECTED_RECORD]:
        assert (record["day_id"], record["site_id"], record["date"]) == (
            ex.SUBMITTED_DAY["id"],
            ex.SUBMITTED_DAY["site_id"],
            ex.SUBMITTED_DAY["date"],
        )


def test_payout_adds_up():
    payout = ex.PAYOUT
    assert payout["total_to_pay"] == sum(line["net"] for line in payout["lines"])
    paid = [line["payment"] for line in payout["lines"] if line["payment"]]
    assert payout["paid_total"] == sum(p["amount"] for p in paid)
    assert payout["left_to_pay"] == payout["total_to_pay"] - payout["paid_total"]
    assert payout["cash_in_hand"] == payout["cash_issued"] - payout["paid_total"]
    assert (payout["line_count"], payout["paid_count"]) == (len(payout["lines"]), len(paid))
    paid_line = next(line for line in payout["lines"] if line["payment"])
    assert paid_line["pay_line_id"] == ex.PAYMENT["pay_line_id"]
    assert paid_line["net"] == ex.PAYMENT["amount"]


def test_the_timeline_is_plausible():
    order = [
        ex.MARKED_AT,
        ex.SUBMITTED_ON_PHONE_AT,
        ex.SUBMITTED_AT,
        ex.SYNCED_SINCE,
        ex.CORRECTED_AT,
        ex.APPROVED_AT,
        ex.SERVER_TIME,
        ex.PAID_AT,
    ]
    assert [_ts(t) for t in order] == sorted(_ts(t) for t in order)
    assert _ts(ex.PREVIOUS_DAY["submitted_at"]) < _ts(ex.PREVIOUS_DAY["approved_at"])
    assert ex.PAYOUT["approved_at"] == ex.APPROVED_DAY["approved_at"]
