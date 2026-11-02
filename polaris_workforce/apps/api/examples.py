"""One coherent data set for every example in the OpenAPI contract.

Every ``OpenApiExample`` in ``apps/<app>/api/`` builds on these values, so the ids,
names and times agree across endpoints (the Flutter mock follows the same story):

* Site 3 "Riverside Apartments" in Lomé (``Africa/Lome``, UTC+0, so ``Z`` times are
  also site times). Foreman Kossi Amegah (user 7), site manager Edem Kpodar (user 4).
* Crew of four on Saturday 2026-10-31: Delali Agbo (63, painter), Kodjo Atsu (58,
  labourer, absent the two days before), Afi Dosseh (51, labourer), Yao Mensah (44,
  mason, repaying an advance).
* 07:41:12 the roll call is saved (day 210, records 9001–9004); 07:42:01 on the phone
  clock the foreman taps Submit, which reaches the server at 07:42:05; 09:15 he
  corrects Delali's arrival; 16:10 Edem approves; 16:12:30 the phone syncs; 16:31 Yao
  is paid in cash.
* The day before (day 209, 2026-10-30) was approved and fully paid.
"""

DATE = "2026-10-31"
PREVIOUS_DATE = "2026-10-30"
SITE_ID = 3
DAY_ID = 210
PREVIOUS_DAY_ID = 209

FOREMAN = {"id": 7, "name": "Kossi Amegah"}
MANAGER = {"id": 4, "name": "Edem Kpodar"}

USER = {
    "id": FOREMAN["id"],
    "name": FOREMAN["name"],
    "role": "foreman",
    "phone": "+22890112233",
    "language": "fr",
}

SITE = {
    "id": SITE_ID,
    "name": "Riverside Apartments",
    "address": "Boulevard du Mono, Lomé",
    "manager": MANAGER,
    "work_start": "07:00",
    "roll_call_due": "08:00",
}

XOF = {"code": "XOF", "decimals": 0}

COMPANY = {
    "country": "TG",
    "time_zone": "Africa/Lome",
    "currency": XOF,
    "payments": {
        "accepted_proofs": ["photo", "signature", "thumb_mark"],
        "file_required_for": ["photo"],
    },
}

DEVICE = {"id": 12, "device_id": "0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11", "name": "Tecno Spark 10"}
TOKEN = "q9V3rM2xQk7f0bU8p1sZcW4yL6tN5hJaDgE_oRiK-Yw"

MEDIA = "https://polaris.example.com/media"

# Workers, sorted by last name as the crew list and the cash sheet sort them.
DELALI = {"id": 63, "number": "W-0163", "first_name": "Delali", "last_name": "Agbo"}
KODJO = {"id": 58, "number": "W-0157", "first_name": "Kodjo", "last_name": "Atsu"}
AFI = {"id": 51, "number": "W-0142", "first_name": "Afi", "last_name": "Dosseh"}
YAO = {"id": 44, "number": "W-0118", "first_name": "Yao", "last_name": "Mensah"}
TRADES = {63: "Painter", 58: "Labourer", 51: "Labourer", 44: "Mason"}


def full_name(worker: dict) -> str:
    return f"{worker['first_name']} {worker['last_name']}"


def photo_url(worker: dict) -> str:
    return f"{MEDIA}/workers/{worker['number']}.jpg"


# --- Attendance ----------------------------------------------------------------------------

MARKED_AT = f"{DATE}T07:41:12Z"
SUBMITTED_ON_PHONE_AT = f"{DATE}T07:42:01Z"
SUBMITTED_AT = f"{DATE}T07:42:05Z"
CORRECTED_AT = f"{DATE}T09:15:00Z"
APPROVED_AT = f"{DATE}T16:10:00Z"
SYNCED_SINCE = f"{DATE}T07:45:00Z"
SERVER_TIME = f"{DATE}T16:12:30Z"
PAID_AT = f"{DATE}T16:31:00Z"

SUBMIT_UUID = "c0a8012e-4b7d-4f3e-9a1b-2d3c4e5f6a71"
FOREMAN_NOTE = "Concrete delivery late, Delali started 07:35."


def record(rid, uuid, worker, status, arrival, departure=None, **extra):
    return {
        "id": rid,
        "client_uuid": uuid,
        "day_id": DAY_ID,
        "site_id": SITE_ID,
        "date": DATE,
        "worker_id": worker["id"],
        "status": status,
        "arrival": arrival,
        "departure": departure,
        "note": "",
        "corrected": False,
        "updated_at": MARKED_AT,
        **extra,
    }


RECORDS = [
    record(9001, "3f2a7c1e-5b8d-4e0a-9c61-0d4b8e2f7a10", YAO, "present", "07:00"),
    record(9002, "8c4d1b2e-7f3a-4a9b-b2c5-6e1f0a3d9b21", AFI, "half_day", "07:00", "12:00"),
    record(9003, "a1e5f9c3-2d4b-4f7e-8a06-b3c2d1e0f932", DELALI, "late", "07:35"),
    record(9004, "d7b3e2a1-9c8f-4b5d-a6e0-1f2c3b4a5d43", KODJO, "absent", None),
]
CORRECTED_RECORD = {**RECORDS[2], "arrival": "07:20", "corrected": True, "updated_at": CORRECTED_AT}

COUNTS = {"expected": 4, "present": 1, "absent": 1, "half_day": 1, "late": 1, "unmarked": 0}

# Day 210 just after it was submitted.
SUBMITTED_DAY = {
    "id": DAY_ID,
    "site_id": SITE_ID,
    "date": DATE,
    "status": "submitted",
    "foreman_note": FOREMAN_NOTE,
    "submitted_at": SUBMITTED_AT,
    "submitted_by": FOREMAN,
    "approved_at": None,
    "approved_by": None,
    "rejection_note": "",
    "counts": COUNTS,
    "payout_status": "not_open",
    "updated_at": SUBMITTED_AT,
}

DRAFT_DAY = {
    **SUBMITTED_DAY,
    "status": "draft",
    "foreman_note": "",
    "submitted_at": None,
    "submitted_by": None,
    "updated_at": MARKED_AT,
}

NOT_STARTED_DAY = {
    **DRAFT_DAY,
    "id": None,
    "status": "not_started",
    "counts": {**COUNTS, "present": 0, "absent": 0, "half_day": 0, "late": 0, "unmarked": 4},
    "updated_at": None,
}

# Day 210 as the phone sees it after the office approved it.
APPROVED_DAY = {
    **SUBMITTED_DAY,
    "status": "approved",
    "approved_at": APPROVED_AT,
    "approved_by": MANAGER,
    "payout_status": "open",
    "updated_at": APPROVED_AT,
}

# Day 209: Kodjo was already absent; approved and everyone paid.
PREVIOUS_DAY = {
    **SUBMITTED_DAY,
    "id": PREVIOUS_DAY_ID,
    "date": PREVIOUS_DATE,
    "status": "approved",
    "foreman_note": "",
    "submitted_at": f"{PREVIOUS_DATE}T07:38:40Z",
    "approved_at": f"{PREVIOUS_DATE}T15:52:00Z",
    "approved_by": MANAGER,
    "counts": {**COUNTS, "present": 3, "absent": 1, "half_day": 0, "late": 0},
    "payout_status": "paid",
    "updated_at": f"{PREVIOUS_DATE}T15:52:00Z",
}

CORRECTION = {
    "id": 77,
    "client_uuid": "5e9d0c4b-3a2f-4e1d-9b8c-7a6f5e4d3c21",
    "record_id": CORRECTED_RECORD["id"],
    "field": "arrival",
    "old_value": "07:35",
    "new_value": "07:20",
    "reason": "Signed in at the gate at 07:20; marked late by mistake.",
    "created_by": FOREMAN,
    "created_at": CORRECTED_AT,
    "record": CORRECTED_RECORD,
}


# --- Crew ----------------------------------------------------------------------------------


def crew_worker(worker, pay_type="daily", absent_streak=0):
    return {
        **worker,
        "trade": TRADES[worker["id"]],
        "photo_url": photo_url(worker),
        "pay_type": pay_type,
        "absent_streak": absent_streak,
    }


CREW = {
    "site_id": SITE_ID,
    "date": DATE,
    "count": 4,
    "workers": [
        crew_worker(DELALI),
        crew_worker(KODJO, absent_streak=2),
        crew_worker(AFI),
        crew_worker(YAO),
    ],
}


# --- Payout --------------------------------------------------------------------------------

PAYMENT_UUID = "e4b1c9d2-6a3f-4d8e-b7c0-1a2b3c4d5e61"
PAYMENT_SUMMARY = {
    "id": 3301,
    "client_uuid": PAYMENT_UUID,
    "amount": 4000,
    "method": "cash",
    "proof_type": "photo",
    "paid_at": PAID_AT,
}


def pay_line(line_id, worker, worked, units, gross, advance=0, payment=None):
    return {
        "pay_line_id": line_id,
        "worker": {
            "id": worker["id"],
            "name": full_name(worker),
            "trade": TRADES[worker["id"]],
            "photo_url": photo_url(worker),
        },
        "worked": worked,
        "units": units,
        "gross": gross,
        "advance_deduction": advance,
        "net": gross - advance,
        "payment": payment,
    }


# Kodjo was absent: no line. Yao repays 2 000 of his advance.
PAY_LINES = [
    pay_line(803, DELALI, "1 day · late", "1.00", 7500),
    pay_line(802, AFI, "½ day", "0.50", 1750),
    pay_line(801, YAO, "1 day", "1.00", 6000, 2000, PAYMENT_SUMMARY),
]

PAYOUT = {
    "site_id": SITE_ID,
    "date": DATE,
    "status": "open",
    "day_status": "approved",
    "approved_at": APPROVED_AT,
    "currency": XOF,
    "cash_issued": 20000,
    "total_to_pay": 13250,
    "paid_total": 4000,
    "left_to_pay": 9250,
    "cash_to_pay": 13250,
    "cash_in_hand": 16000,
    "line_count": 3,
    "paid_count": 1,
    "lines": PAY_LINES,
}

PAYMENT = {
    **PAYMENT_SUMMARY,
    "pay_line_id": 801,
    "currency": XOF,
    "proof_url": f"{MEDIA}/proofs/2026/10/receipt-3301.jpg",
    "paid_by": FOREMAN,
}
