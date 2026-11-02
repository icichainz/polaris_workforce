"""Attendance workflow. Every write to days, records and corrections goes through here.

Each function runs in one short transaction and writes an ``AuditLog`` entry when it
changes something (CLAUDE.md rules 5, 6 and 11). Errors are the API's exceptions, so the
API turns them into its error format and office views can show their message:

* ``Locked`` (423): the day is locked, or submitted/approved for a plain record edit.
* ``Conflict`` (409): the request clashes with the day's state, or a ``client_uuid``
  was already used for something else.
* ``ValidationError`` (400): bad input (missing reason, worker not on the crew…).
* ``PermissionDenied`` (403): the user may not do this on this site.

Day workflow (``TRANSITIONS`` is the only place that says what may follow what)::

    not_started → draft → submitted → approved → locked
                    ↑         │  ↑        │
                    │         ↓  └────────┘ (a foreman corrects an approved day)
                    └──── rejected ──→ submitted
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.accounts.permissions import can_manage_site, can_view_site
from apps.api.exceptions import Conflict, Locked
from apps.api.idempotency import IdempotentResult, apply_changes, run_once
from apps.checkin.selectors import SOURCES as SUGGESTION_SOURCES
from apps.core.dates import company_today
from apps.core.models import AuditLog

from .models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from .selectors import NOT_STARTED, expected_worker_ids, unmarked_worker_ids

Status = AttendanceDay.Status

# --- Status transitions ----------------------------------------------------------------

TRANSITIONS: dict[str, frozenset[str]] = {
    NOT_STARTED: frozenset({Status.DRAFT}),
    Status.DRAFT: frozenset({Status.SUBMITTED}),
    Status.REJECTED: frozenset({Status.DRAFT, Status.SUBMITTED}),
    Status.SUBMITTED: frozenset({Status.APPROVED, Status.REJECTED}),
    Status.APPROVED: frozenset({Status.SUBMITTED, Status.LOCKED}),
    Status.LOCKED: frozenset(),
}
# The foreman edits records directly (batch PUT) only in these states…
EDITABLE = frozenset({Status.DRAFT, Status.REJECTED})
# …and through a correction with a reason in these.
CORRECTABLE = frozenset({Status.SUBMITTED, Status.APPROVED})

RECORD_FIELDS = ("status", "arrival", "departure", "note")
CORRECTABLE_FIELDS = RECORD_FIELDS


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def _move(day: AttendanceDay, target: str) -> None:
    """Change ``day.status`` to ``target`` (unsaved), or raise ``Locked``/``Conflict``."""
    if not can_transition(day.status, target):
        details = {"status": day.status}
        if day.status == Status.LOCKED:
            raise Locked(_("This day is locked and cannot be changed."), details=details)
        raise Conflict(
            _("This day cannot go from %(current)s to %(target)s.")
            % {"current": day.get_status_display(), "target": Status(target).label},
            details=details,
        )
    day.status = target


# --- Audit helpers ---------------------------------------------------------------------


def _text(value: Any) -> str:
    """A record value as stored in corrections and the audit log."""
    if value is None:
        return ""
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    return str(value)


def _record_state(record: AttendanceRecord) -> dict[str, str]:
    return {name: _text(getattr(record, name)) for name in RECORD_FIELDS}


def _day_state(day: AttendanceDay) -> dict[str, Any]:
    return {"status": day.status, "site_id": day.site_id, "date": day.date.isoformat()}


def _check_can_write(user, day: AttendanceDay) -> None:
    if not can_view_site(user, day.site):
        raise PermissionDenied


# --- Days ------------------------------------------------------------------------------


def get_or_create_day(site, date: datetime.date, foreman) -> AttendanceDay:
    """The site's day for ``date``; created as a draft (``not_started`` → ``draft``)."""
    day = AttendanceDay.objects.filter(site=site, date=date).first()
    if day is not None:
        return day
    if date > company_today():
        raise ValidationError({"date": [_("You cannot take the roll call for a future day.")]})
    try:
        with transaction.atomic():
            day = AttendanceDay.objects.create(
                site=site, date=date, foreman=foreman, status=Status.DRAFT
            )
            AuditLog.record(
                actor=foreman, action="attendance.day.create", target=day, after=_day_state(day)
            )
    except IntegrityError:
        # The same day was created by a parallel request (two phones): use it.
        return AttendanceDay.objects.get(site=site, date=date)
    return day


def upsert_records(day: AttendanceDay, records: list[dict], user) -> list[AttendanceRecord]:
    """Create or update the day's records from a phone batch; return the server's records.

    ``records`` holds dicts with ``client_uuid``, ``worker_id``, ``status`` (model value),
    ``arrival``, ``departure``, ``note`` and optionally ``from_suggestion``: the sources
    (``checkin``, ``presence``) of the roll-call suggestion the mark was taken from, empty
    for a mark made by hand. It changes nothing on the record; the audit entry lists it
    per worker (``after["from_suggestion"]``) for the records the batch changed, so
    an accepted presence suggestion is on the record's history. Idempotent: a record is found by
    ``client_uuid``, then by worker (one record per worker per day; the server's
    ``client_uuid`` wins), and only changed fields are written. A batch that changes
    nothing writes nothing, whatever the day's status. Otherwise the day must be
    ``draft`` or ``rejected`` (which goes back to ``draft``), else ``Locked``.
    """
    _check_can_write(user, day)
    try:
        return _upsert_records(day, records, user)
    except IntegrityError:
        # Lost a race with another phone creating the same record: replay on fresh data.
        day.refresh_from_db()
        return _upsert_records(day, records, user)


def _upsert_records(day, records, user):
    expected = expected_worker_ids(day.site, day.date)
    by_worker = {record.worker_id: record for record in day.records.all()}
    uuids = [item["client_uuid"] for item in records]
    by_uuid = {
        # Use the same object as by_worker for this day's records, so each row is saved once.
        record.client_uuid: by_worker.get(record.worker_id, record)
        if record.day_id == day.pk
        else record
        for record in AttendanceRecord.objects.filter(client_uuid__in=uuids)
    }

    errors: list[dict] = []
    for item in records:
        found = by_uuid.get(item["client_uuid"])
        if found is not None and (found.day_id != day.pk or found.worker_id != item["worker_id"]):
            raise Conflict(
                _("This client_uuid was already used for another record."),
                details={"client_uuid": str(item["client_uuid"])},
            )
        if (
            found is None
            and item["worker_id"] not in by_worker
            and item["worker_id"] not in expected
        ):
            errors.append(
                {"worker_id": [_("This worker is not on the crew of this site that day.")]}
            )
        else:
            errors.append({})
    if any(errors):
        raise ValidationError({"records": errors})

    result: list[AttendanceRecord] = []
    to_save: dict[int, tuple[AttendanceRecord, set[str] | None]] = {}  # id(record) → fields
    before: dict[str, dict] = {}
    for item in records:
        values = {name: item.get(name) for name in RECORD_FIELDS}
        values["note"] = values["note"] or ""
        record = by_uuid.get(item["client_uuid"]) or by_worker.get(item["worker_id"])
        if record is None:
            record = AttendanceRecord(
                day=day, worker_id=item["worker_id"], client_uuid=item["client_uuid"], **values
            )
            by_worker[record.worker_id] = record
            by_uuid[record.client_uuid] = record
            to_save[id(record)] = (record, None)
            before[str(record.worker_id)] = {}
        else:
            old = _record_state(record)
            changed = apply_changes(record, values, RECORD_FIELDS)
            if changed:
                previous = to_save.get(id(record), (record, set()))[1]
                fields = None if previous is None else previous | set(changed)
                to_save[id(record)] = (record, fields)
                seen = before.setdefault(str(record.worker_id), {})
                for name in changed:
                    seen.setdefault(name, old[name])
        if record not in result:
            result.append(record)

    if not to_save:
        return result
    if day.status not in EDITABLE:
        raise Locked(
            _("This day was already submitted: change records with a correction."),
            details={"status": day.status},
        )
    with transaction.atomic():
        old_status = day.status
        if day.status == Status.REJECTED:
            _move(day, Status.DRAFT)
        # New records in one INSERT (a first roll call is the whole crew).
        AttendanceRecord.objects.bulk_create(
            [record for record, changed in to_save.values() if changed is None]
        )
        for record, changed in to_save.values():
            if changed is not None:
                record.save(update_fields=[*sorted(changed), "updated_at"])
        day.save(update_fields=["status", "updated_at"])  # bumps updated_at for sync
        after = {
            "status": day.status,
            "records": {
                str(record.worker_id): _record_state(record) for record, _f in to_save.values()
            },
        }
        suggested = _from_suggestion(records, {record.worker_id for record, _f in to_save.values()})
        if suggested:
            after["from_suggestion"] = suggested
        AuditLog.record(
            actor=user,
            action="attendance.records.upsert",
            target=day,
            before={"status": old_status, "records": before},
            after=after,
        )
    return result


def _from_suggestion(records: list[dict], changed: set[int]) -> dict[str, list[str]]:
    """``{worker id: sources}`` of the changed records whose mark came from a suggestion
    (the last item of a worker in the batch decides, as for the values)."""
    found: dict[str, list[str]] = {}
    for item in records:
        if item["worker_id"] not in changed:
            continue
        sources = set(item.get("from_suggestion") or ())
        key = str(item["worker_id"])
        if sources:
            found[key] = [source for source in SUGGESTION_SOURCES if source in sources]
        else:
            found.pop(key, None)
    return found


def submit_day(
    day: AttendanceDay,
    user,
    client_uuid: uuid.UUID,
    note: str = "",
    client_submitted_at: datetime.datetime | None = None,
) -> AttendanceDay:
    """``draft``/``rejected`` → ``submitted``. Every expected worker must be marked.

    Idempotent: the same ``client_uuid`` again returns the day as it is now. Another
    ``client_uuid`` on a submitted or approved day is a ``Conflict``; a locked day is
    ``Locked``; unmarked workers give ``Conflict`` with ``unmarked_worker_ids``.
    """
    _check_can_write(user, day)
    if day.client_uuid == client_uuid and day.submitted_at is not None:
        return day
    if AttendanceDay.objects.filter(client_uuid=client_uuid).exclude(pk=day.pk).exists():
        raise Conflict(
            _("This client_uuid was already used for a different request."),
            details={"client_uuid": str(client_uuid)},
        )
    if day.status == Status.LOCKED:
        raise Locked(_("This day is locked and cannot be changed."), details={"status": day.status})
    if day.status not in (Status.DRAFT, Status.REJECTED):
        raise Conflict(_("This day was already submitted."), details={"status": day.status})
    missing = unmarked_worker_ids(day)
    if missing:
        raise Conflict(
            _("Mark every worker on the crew before submitting."),
            details={"unmarked_worker_ids": missing},
        )
    with transaction.atomic():
        old_status = day.status
        _move(day, Status.SUBMITTED)
        day.submitted_at = timezone.now()
        day.submitted_by = user
        day.client_submitted_at = client_submitted_at
        day.client_uuid = client_uuid
        day.foreman_note = (note or "").strip()
        day.rejection_note = ""
        day.save()
        AuditLog.record(
            actor=user,
            action="attendance.day.submit",
            target=day,
            before={"status": old_status},
            after={"status": day.status, "foreman_note": day.foreman_note},
        )
    return day


def approve_day(day: AttendanceDay, user) -> AttendanceDay:
    """``submitted`` → ``approved``. Admins, or the site's manager."""
    if not can_manage_site(user, day.site):
        raise PermissionDenied
    with transaction.atomic():
        old_status = day.status
        _move(day, Status.APPROVED)
        day.approved_at = timezone.now()
        day.approved_by = user
        day.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
        AuditLog.record(
            actor=user,
            action="attendance.day.approve",
            target=day,
            before={"status": old_status},
            after={"status": day.status},
        )
    _notify_payroll(day, user)
    return day


def reject_day(day: AttendanceDay, user, note: str) -> AttendanceDay:
    """``submitted`` → ``rejected``: back to the foreman, editable again. Note required."""
    if not can_manage_site(user, day.site):
        raise PermissionDenied
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": [_("Tell the foreman what to check.")]})
    with transaction.atomic():
        old_status = day.status
        _move(day, Status.REJECTED)
        day.rejection_note = note
        day.approved_at = None
        day.approved_by = None
        day.save(
            update_fields=["status", "rejection_note", "approved_at", "approved_by", "updated_at"]
        )
        AuditLog.record(
            actor=user,
            action="attendance.day.reject",
            target=day,
            before={"status": old_status},
            after={"status": day.status},
            reason=note,
        )
    _notify_payroll(day, user)
    return day


def lock_day(day: AttendanceDay, user=None) -> AttendanceDay:
    """``approved`` → ``locked`` (called by payroll). Locking a locked day does nothing."""
    if day.status == Status.LOCKED:
        return day
    with transaction.atomic():
        old_status = day.status
        _move(day, Status.LOCKED)
        day.save(update_fields=["status", "updated_at"])
        AuditLog.record(
            actor=user,
            action="attendance.day.lock",
            target=day,
            before={"status": old_status},
            after={"status": day.status},
        )
    return day


# --- Corrections -----------------------------------------------------------------------


def _parse_value(field: str, raw):
    if field == "status":
        if raw not in AttendanceRecord.Status.values:
            raise ValidationError({"new_value": [_("Not a valid attendance status.")]})
        return raw
    if field in ("arrival", "departure"):
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime.time):
            return raw
        try:
            return datetime.datetime.strptime(str(raw), "%H:%M").time()
        except ValueError as exc:
            raise ValidationError({"new_value": [_("Use the 24-hour format HH:MM.")]}) from exc
    return raw or ""


def correct_record(
    record: AttendanceRecord,
    field: str,
    new_value,
    reason: str,
    user,
    client_uuid: uuid.UUID,
) -> IdempotentResult[AttendanceCorrection]:
    """Change one field of a record after submission, with a reason.

    Allowed while the day is ``submitted`` or ``approved`` (``draft``/``rejected``:
    ``Conflict``, edit the record directly; ``locked``: ``Locked``). Writes an
    ``AttendanceCorrection`` and an ``AuditLog`` entry and marks the record corrected.
    A correction by someone who cannot approve the site sends an approved day back to
    ``submitted`` for review. Idempotent by ``client_uuid`` (``created`` is False on a
    replay; the same ``client_uuid`` for another record or field is a ``Conflict``).
    """
    _check_can_write(user, record.day)
    result = run_once(
        AttendanceCorrection.objects,
        client_uuid,
        create=lambda: _create_correction(record, field, new_value, reason, user, client_uuid),
        matches=lambda c: c.record_id == record.pk and c.field == field,
    )
    if result.created:
        _notify_payroll(record.day, user)
    return result


def _notify_payroll(day: AttendanceDay, user) -> None:
    """Pay is computed from approved attendance: let payroll refresh this day's pay lines."""
    # Imported here: payroll's services import this module.
    from apps.payroll.services import on_attendance_changed

    on_attendance_changed(day, actor=user)


def _create_correction(record, field, new_value, reason, user, client_uuid):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": [_("Give a reason for the correction.")]})
    if field not in CORRECTABLE_FIELDS:
        raise ValidationError({"field": [_("This field cannot be corrected.")]})
    day = record.day
    if day.status == Status.LOCKED:
        raise Locked(_("This day is locked and cannot be changed."), details={"status": day.status})
    if day.status not in CORRECTABLE:
        raise Conflict(
            _("This day is not submitted yet: change the record directly."),
            details={"status": day.status},
        )
    value = _parse_value(field, new_value)
    old = getattr(record, field)
    if old == value:
        raise ValidationError({"new_value": [_("The record already has this value.")]})

    correction = AttendanceCorrection.objects.create(
        record=record,
        field=field,
        old_value=_text(old),
        new_value=_text(value),
        reason=reason,
        corrected_by=user,
        client_uuid=client_uuid,
    )
    setattr(record, field, value)
    record.corrected = True
    record.save(update_fields=[field, "corrected", "updated_at"])

    old_status = day.status
    if day.status == Status.APPROVED and not can_manage_site(user, day.site):
        _move(day, Status.SUBMITTED)
        day.approved_at = None
        day.approved_by = None
    day.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    AuditLog.record(
        actor=user,
        action="attendance.record.correct",
        target=record,
        before={field: correction.old_value, "day_status": old_status},
        after={field: correction.new_value, "day_status": day.status},
        reason=reason,
    )
    return correction
