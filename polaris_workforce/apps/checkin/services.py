"""Check-in writes: badges, geofences, kiosk phones and check-in events.

Views check who may act (scoped querysets, ``can_manage_site``); these functions do the
work in short transactions and write an ``AuditLog`` entry for every office action
(badge issued or revoked, geofence changed, kiosk mode on or off). A check-in itself is
its own record; when it changes the roll call (``self_check_in`` mode) it goes through
``apps.attendance.services.upsert_records``, which writes the attendance audit entry.

Attendance modes (``CompanySettings.attendance_mode``):

* ``foreman_roll_call``: check-ins are recorded and shown as suggestions; nothing else.
* ``both``: the same — suggestions pre-fill the roll call on the phone.
* ``self_check_in``: each check-in also writes the worker's record on the day's
  **draft** (as the scanning phone's user), never on a submitted, approved, rejected or
  locked day. The foreman still reviews and submits.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ForemanDevice, User
from apps.accounts.permissions import can_manage_site, scoped_sites
from apps.api.exceptions import Conflict, Locked
from apps.api.idempotency import IdempotentResult, run_once
from apps.attendance import services as attendance_services
from apps.attendance.models import AttendanceDay
from apps.core.choices import AttendanceMode
from apps.core.dates import company_today
from apps.core.models import AuditLog
from apps.worksites.services import crew_ids

from . import geo, selectors, tokens
from .models import Badge, CheckIn, KioskDevice, SiteGeofence

MAX_CLOCK_AHEAD = datetime.timedelta(minutes=10)
MAX_AGE = datetime.timedelta(days=31)
# Records written by check-ins get a client_uuid derived from (site, date, worker), so
# two racing check-ins agree on it.
RECORD_UUID_NAMESPACE = uuid.UUID("5b0b6f8e-7c1d-4d39-9a8e-1f2b3c4d5e60")


# --- badges -----------------------------------------------------------------------------


def _badge_state(badge: Badge) -> dict:
    return {
        "worker_id": badge.worker_id,
        "public_id": badge.public_id,
        "revoked": badge.revoked_at is not None,
    }


def active_badge(worker) -> Badge | None:
    return Badge.objects.filter(worker=worker, revoked_at__isnull=True).first()


def issue_badge(worker, actor) -> Badge:
    """A new badge for ``worker``; the active one, if any, is revoked ("Reissued")."""
    with transaction.atomic():
        old = active_badge(worker)
        if old is not None:
            _revoke(old, actor, _("Reissued"))
        badge = Badge.objects.create(worker=worker, issued_by=actor)
        AuditLog.record(
            actor=actor,
            action="checkin.badge.issue",
            target=badge,
            before=_badge_state(old) if old else None,
            after=_badge_state(badge),
            company=worker.company_id,
        )
    return badge


def issue_missing_badges(workers, actor) -> list[Badge]:
    """A badge for each of ``workers`` without an active one."""
    has_badge = set(
        Badge.objects.filter(worker__in=workers, revoked_at__isnull=True).values_list(
            "worker_id", flat=True
        )
    )
    return [issue_badge(worker, actor) for worker in workers if worker.pk not in has_badge]


def _revoke(badge: Badge, actor, reason: str) -> None:
    badge.revoked_at = timezone.now()
    badge.revoked_by = actor
    badge.revoke_reason = str(reason)[:200]
    badge.save(update_fields=["revoked_at", "revoked_by", "revoke_reason"])


def revoke_badge(badge: Badge, actor, reason: str = "") -> Badge:
    """Stop a badge from counting (lost, stolen, worker left). Revoking twice does nothing."""
    if badge.revoked_at is not None:
        return badge
    with transaction.atomic():
        before = _badge_state(badge)
        _revoke(badge, actor, reason.strip())
        AuditLog.record(
            actor=actor,
            action="checkin.badge.revoke",
            target=badge,
            before=before,
            after=_badge_state(badge),
            reason=reason.strip(),
            company=badge.worker.company_id,
        )
    return badge


def find_badge(site, scanned: str) -> tuple[Badge | None, str]:
    """``(badge, problem)`` for a scanned QR text on ``site``.

    A text that is not a correctly signed badge, or the badge of another company, is
    ``(None, "unknown")``; a revoked badge is ``(badge, "revoked")``.
    """
    public_id = tokens.verify(scanned)
    badge = None
    if public_id is not None:
        badge = (
            Badge.objects.select_related("worker")
            .filter(public_id=public_id, worker__company_id=site.company_id)
            .first()
        )
    if badge is None:
        return None, CheckIn.BadgeProblem.UNKNOWN
    if badge.revoked_at is not None:
        return badge, CheckIn.BadgeProblem.REVOKED
    return badge, CheckIn.BadgeProblem.NONE


# --- geofences --------------------------------------------------------------------------


def _geofence_state(geofence: SiteGeofence | None) -> dict | None:
    if geofence is None:
        return None
    return {
        "latitude": str(geofence.latitude),
        "longitude": str(geofence.longitude),
        "radius_m": geofence.radius_m,
    }


def set_geofence(site, *, latitude: Decimal, longitude: Decimal, radius_m: int, actor):
    if not can_manage_site(actor, site):
        raise PermissionDenied
    with transaction.atomic():
        geofence = SiteGeofence.objects.filter(site=site).first()
        before = _geofence_state(geofence)
        if geofence is None:
            geofence = SiteGeofence(site=site)
        geofence.latitude, geofence.longitude, geofence.radius_m = latitude, longitude, radius_m
        geofence.updated_by = actor
        geofence.full_clean()
        geofence.save()
        geofence.refresh_from_db()  # decimals as stored
        AuditLog.record(
            actor=actor,
            action="checkin.geofence.set",
            target=site,
            before=before,
            after=_geofence_state(geofence),
            company=site.company_id,
        )
    return geofence


def clear_geofence(site, actor) -> None:
    if not can_manage_site(actor, site):
        raise PermissionDenied
    geofence = SiteGeofence.objects.filter(site=site).first()
    if geofence is None:
        return
    with transaction.atomic():
        before = _geofence_state(geofence)
        geofence.delete()
        AuditLog.record(
            actor=actor,
            action="checkin.geofence.clear",
            target=site,
            before=before,
            after=None,
            company=site.company_id,
        )


# --- kiosk phones ---------------------------------------------------------------------------


def kiosk_candidate_sites(device: ForemanDevice, actor):
    """Sites ``device`` may become a kiosk for: sites ``actor`` manages on which the
    device's foreman works."""
    sites = scoped_sites(actor).filter(pk__in=scoped_sites(device.user))
    return [site for site in sites.order_by("name") if can_manage_site(actor, site)]


def enable_kiosk(device: ForemanDevice, site, actor) -> KioskDevice:
    """Switch a foreman's phone to kiosk mode for ``site`` (or move it to another site)."""
    if not can_manage_site(actor, site):
        raise PermissionDenied
    if device.revoked_at is not None:
        raise ValidationError({"device": [_("This phone is signed out. Sign in on it first.")]})
    if device.user.role != User.Role.FOREMAN or not device.user.is_active:
        raise ValidationError({"device": [_("Only a foreman's phone can be a kiosk.")]})
    if not scoped_sites(device.user).filter(pk=site.pk).exists():
        raise ValidationError({"site": [_("This phone's foreman does not work on this site.")]})
    with transaction.atomic():
        kiosk = KioskDevice.objects.filter(device=device).first()
        before = {"site_id": kiosk.site_id} if kiosk else None
        if kiosk is None:
            kiosk = KioskDevice(device=device)
        kiosk.site = site
        kiosk.enabled_by = actor
        kiosk.save()
        AuditLog.record(
            actor=actor,
            action="checkin.kiosk.enable",
            target=device,
            before=before,
            after={"site_id": site.pk},
            company=site.company_id,
        )
    return kiosk


def disable_kiosk(kiosk: KioskDevice, actor) -> None:
    """Back to an ordinary foreman phone."""
    if not can_manage_site(actor, kiosk.site):
        raise PermissionDenied
    with transaction.atomic():
        AuditLog.record(
            actor=actor,
            action="checkin.kiosk.disable",
            target=kiosk.device,
            before={"site_id": kiosk.site_id},
            after=None,
            company=kiosk.site.company_id,
        )
        kiosk.delete()


# --- check-ins --------------------------------------------------------------------------


def record_checkin(
    *,
    user,
    device: ForemanDevice | None,
    site,
    direction: str,
    at: datetime.datetime,
    client_uuid: uuid.UUID,
    badge: str | None = None,
    worker=None,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
    accuracy_m: int | None = None,
    photo=None,
) -> IdempotentResult[CheckIn]:
    """Record a check-in on ``site`` (which ``user`` may see; the view checked).

    Exactly one of ``badge`` (the scanned QR text) and ``worker`` (a manual entry by the
    foreman, already scoped to the company) is given. A kiosk phone may only record
    badge scans for its own site (``PermissionDenied`` otherwise). An unknown or revoked
    badge is recorded too, flagged, and never counts. Idempotent by ``client_uuid``
    (``created`` is False on a replay; the same ``client_uuid`` for another site,
    direction, time, worker or user is a ``Conflict``).
    """
    kiosk = selectors.kiosk_of(device)
    if kiosk is not None:
        if kiosk.site_id != site.pk:
            raise exceptions.PermissionDenied(_("This phone is a kiosk for another site."))
        if badge is None:
            raise ValidationError({"badge": [_("A kiosk only records badge scans.")]})
    scanned = badge.strip() if badge is not None else None

    def matches(existing: CheckIn) -> bool:
        same_who = (
            existing.worker_id == worker.pk
            if worker is not None
            else existing.badge_id is not None or existing.scanned_text != ""
        )
        return (
            existing.site_id == site.pk
            and existing.direction == direction
            and existing.at == at
            and existing.recorded_by_id == user.pk
            and same_who
        )

    return run_once(
        CheckIn.objects,
        client_uuid,
        create=lambda: _create_checkin(
            user=user,
            device=device,
            kiosk=kiosk,
            site=site,
            direction=direction,
            at=at,
            client_uuid=client_uuid,
            scanned=scanned,
            worker=worker,
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
            photo=photo,
        ),
        matches=matches,
    )


def _create_checkin(
    *,
    user,
    device,
    kiosk,
    site,
    direction,
    at,
    client_uuid,
    scanned,
    worker,
    latitude,
    longitude,
    accuracy_m,
    photo,
) -> CheckIn:
    settings, tz, _late_after = selectors.site_rules(site)
    now = timezone.now()
    if at > now + MAX_CLOCK_AHEAD:
        raise ValidationError({"at": [_("This time is in the future. Check the phone's clock.")]})
    if at < now - MAX_AGE:
        raise ValidationError({"at": [_("This check-in is more than 31 days old.")]})
    date = at.astimezone(tz).date()

    checkin = CheckIn(
        site=site,
        date=date,
        direction=direction,
        at=at,
        client_uuid=client_uuid,
        device=device,
        recorded_by=user,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m if latitude is not None else None,
    )
    if scanned is not None:
        found, problem = find_badge(site, scanned)
        checkin.badge = found
        checkin.worker = found.worker if found else None
        checkin.badge_problem = problem
        checkin.scanned_text = scanned[: tokens.MAX_SCAN_LENGTH] if found is None else ""
        checkin.source = CheckIn.Source.KIOSK if kiosk else CheckIn.Source.FOREMAN_SCAN
    else:
        checkin.worker = worker
        checkin.source = CheckIn.Source.MANUAL
    if checkin.worker_id is not None:
        checkin.on_crew = checkin.worker_id in crew_ids(site, date)
    geofence = SiteGeofence.objects.filter(site=site).first()
    checkin.distance_m, checkin.inside_geofence = geo.check(
        geofence, latitude, longitude, checkin.accuracy_m
    )
    if photo is not None:
        checkin.photo = photo
    checkin.save()

    if settings.attendance_mode == AttendanceMode.SELF_CHECK_IN and _update_draft(
        checkin, user, today=company_today(settings)
    ):
        checkin.draft_updated = True
        checkin.save(update_fields=["draft_updated"])
    return checkin


def _update_draft(checkin: CheckIn, user, *, today: datetime.date) -> bool:
    """``self_check_in`` mode: put this check-in on the worker's draft record.

    Only while the day is not started or a draft, for a counted check-in of a crew
    worker. A field (status, arrival, departure) is written when the record is new, when
    it is empty (times), or when it still holds what the earlier check-ins suggested — so
    a mark the foreman changed by hand is never overwritten. Returns True when the
    record was written.
    """
    if not checkin.counted or not checkin.on_crew or checkin.date > today:
        return False
    site, date = checkin.site, checkin.date
    day = AttendanceDay.objects.filter(site=site, date=date).first()
    if day is not None and day.status != AttendanceDay.Status.DRAFT:
        return False
    new = selectors.worker_suggestion(site, date, checkin.worker_id)
    if new is None:
        return False
    previous = selectors.worker_suggestion(site, date, checkin.worker_id, exclude_pk=checkin.pk)
    record = day.records.filter(worker_id=checkin.worker_id).first() if day else None
    if record is None and new.status is None:
        return False  # only an "out": the foreman decides the status

    item = {
        "client_uuid": (
            record.client_uuid
            if record
            else uuid.uuid5(RECORD_UUID_NAMESPACE, f"{site.pk}:{date}:{checkin.worker_id}")
        ),
        "worker_id": checkin.worker_id,
        "note": record.note if record else "",
    }
    for name in ("status", "arrival", "departure"):
        value = getattr(new, name)
        if record is None:
            item[name] = value
            continue
        current = getattr(record, name)
        earlier = getattr(previous, name) if previous else None
        if value is not None and (current is None or current == earlier):
            item[name] = value
        else:
            item[name] = current
    try:
        with transaction.atomic():
            day = attendance_services.get_or_create_day(site, date, user)
            if day.status != AttendanceDay.Status.DRAFT:
                return False
            attendance_services.upsert_records(day, [item], user)
    except (Locked, Conflict, ValidationError, PermissionDenied, exceptions.PermissionDenied):
        # The day moved on in the meantime (submitted by another phone…): the check-in
        # stands, the roll call is the foreman's.
        return False
    return True
