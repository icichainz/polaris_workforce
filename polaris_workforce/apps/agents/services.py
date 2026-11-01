"""Agent tokens and roll-call decisions taken through an AI assistant.

Tokens: an office user (admin, site manager, accountant) creates a token for themselves,
sees it once, and may revoke it; an admin may revoke any token of the company. Each of
these writes an ``AuditLog`` entry.

Decisions (approve a submitted day, or send it back to the foreman with a note) always
take two calls, so the person sees what they are about to approve:

1. ``preview_decision`` checks everything the decision needs and answers what the day
   holds plus a signed ``confirmation``. Nothing changes.
2. ``confirm_decision`` takes only that confirmation. The decision and its note are the
   previewed ones (they are inside the signature). It fails if the confirmation is
   older than ``CONFIRMATION_MAX_AGE``, was made for another token, or if the day
   changed after the preview (a correction, another approval): preview again.

The decision itself goes through ``apps.attendance.services`` (same rules and audit
entry as the Approvals screen). An extra ``agents.day.approve`` / ``agents.day.reject``
entry names the token, so the audit log tells assistant decisions apart. Sending the same
confirmation again answers the current day instead of deciding twice (CLAUDE.md rule 7).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from apps.accounts.permissions import (
    APPROVE_DAYS,
    Role,
    can,
    can_manage_site,
    has_role,
    is_admin,
    scoped_sites,
)
from apps.api.exceptions import Conflict
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog

from .models import AgentToken

TOKEN_PREFIX = "pol_"
TOKEN_BYTES = 32
SHOWN_PREFIX_LENGTH = 10  # "pol_" and six characters
LIFETIMES_DAYS = (30, 90, 365)
DEFAULT_LIFETIME_DAYS = 90
OFFICE_ROLES = (Role.ADMIN, Role.SITE_MANAGER, Role.ACCOUNTANT)

APPROVE = "approve"
REJECT = "reject"
DECISIONS = (APPROVE, REJECT)
CONFIRMATION_MAX_AGE = datetime.timedelta(minutes=10)
CONFIRMATION_SALT = "apps.agents.decision"


# --- tokens ----------------------------------------------------------------------------


def may_hold_token(user) -> bool:
    """Office roles of a company. Foremen use the phone; platform admins pick a company in
    the browser, which a token cannot do."""
    return has_role(user, *OFFICE_ROLES) and user.company_id is not None


def may_approve(user) -> bool:
    return can(user, APPROVE_DAYS)


def manageable_tokens(user):
    """Tokens ``user`` may see and revoke: their own; an admin, every one of the company."""
    if not may_hold_token(user):
        return AgentToken.objects.none()
    tokens = AgentToken.objects.filter(company_id=user.company_id).select_related("user")
    return tokens if is_admin(user) else tokens.filter(user=user)


def _token_state(token: AgentToken) -> dict:
    return {
        "name": token.name,
        "prefix": token.prefix,
        "user_id": token.user_id,
        "can_approve": token.can_approve,
        "expires_at": token.expires_at.isoformat(),
    }


def create_token(
    user, *, name: str, can_approve: bool = False, lifetime_days: int = DEFAULT_LIFETIME_DAYS
) -> tuple[AgentToken, str]:
    """Create a token for ``user``. Returns ``(token, raw)``: show ``raw`` once, never keep it."""
    if not may_hold_token(user):
        raise PermissionDenied
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": [_("Give the token a name.")]})
    if lifetime_days not in LIFETIMES_DAYS:
        raise ValidationError({"lifetime_days": [_("Choose one of the offered durations.")]})
    if can_approve and not may_approve(user):
        raise ValidationError({"can_approve": [_("Your role may not approve roll calls.")]})
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    with transaction.atomic():
        token = AgentToken.objects.create(
            company_id=user.company_id,
            user=user,
            name=name,
            prefix=raw[:SHOWN_PREFIX_LENGTH],
            token_hash=AgentToken.hash_token(raw),
            can_approve=can_approve,
            expires_at=timezone.now() + datetime.timedelta(days=lifetime_days),
        )
        AuditLog.record(
            actor=user, action="agents.token.create", target=token, after=_token_state(token)
        )
    return token, raw


def revoke_token(actor, token: AgentToken) -> bool:
    """Revoke ``token``. False if it was already revoked. 403 if ``actor`` may not."""
    if not manageable_tokens(actor).filter(pk=token.pk).exists():
        raise PermissionDenied
    with transaction.atomic():
        updated = AgentToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        if updated:
            AuditLog.record(
                actor=actor,
                action="agents.token.revoke",
                target=token,
                before=_token_state(token),
                after={"revoked": True},
            )
    return bool(updated)


# --- decisions -------------------------------------------------------------------------


def day_fingerprint(day: AttendanceDay) -> str:
    """Changes whenever the day or one of its records changes (status, times, notes)."""
    records = day.records.order_by("pk").values_list(
        "pk", "worker_id", "status", "arrival", "departure", "note", "corrected"
    )
    payload = [
        day.status,
        day.updated_at.isoformat() if day.updated_at else "",
        [[str(value) for value in record] for record in records],
    ]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def scoped_day(user, day_id) -> AttendanceDay:
    """A day on one of the user's sites, else 403 (a missing id too, so ids can't be probed)."""
    day = (
        AttendanceDay.objects.filter(pk=day_id, site__in=scoped_sites(user))
        .select_related("site", "foreman", "submitted_by", "approved_by")
        .first()
    )
    if day is None:
        raise PermissionDenied
    return day


def _check_may_decide(token: AgentToken, day: AttendanceDay) -> None:
    if not token.can_approve or not can_manage_site(token.user, day.site):
        raise PermissionDenied


@dataclass
class DecisionPreview:
    day: AttendanceDay
    decision: str
    note: str
    confirmation: str
    expires_at: datetime.datetime


def preview_decision(token: AgentToken, day: AttendanceDay, decision: str, note: str = ""):
    """Check that ``decision`` can be taken on ``day`` now; nothing is written."""
    _check_may_decide(token, day)
    if decision not in DECISIONS:
        raise ValidationError({"decision": [_("Choose approve or reject.")]})
    if day.status != AttendanceDay.Status.SUBMITTED:
        raise Conflict(
            _("Only a submitted roll call can be approved or sent back. This one is %(status)s.")
            % {"status": day.get_status_display()},
            details={"status": day.status},
        )
    note = (note or "").strip()
    if decision == REJECT and not note:
        raise ValidationError({"note": [_("Tell the foreman what to check.")]})
    payload = {
        "token": token.pk,
        "day": day.pk,
        "decision": decision,
        "note": note,
        "fingerprint": day_fingerprint(day),
        "nonce": uuid.uuid4().hex,
    }
    return DecisionPreview(
        day=day,
        decision=decision,
        note=note,
        confirmation=signing.dumps(payload, salt=CONFIRMATION_SALT, compress=True),
        expires_at=timezone.now() + CONFIRMATION_MAX_AGE,
    )


def _invalid_confirmation(message):
    return ValidationError({"confirmation": [message]})


def _read_confirmation(token: AgentToken, confirmation: str) -> dict:
    try:
        data = signing.loads(
            confirmation, salt=CONFIRMATION_SALT, max_age=CONFIRMATION_MAX_AGE.total_seconds()
        )
    except signing.SignatureExpired as exc:
        raise _invalid_confirmation(
            _("This confirmation has expired. Preview the decision again.")
        ) from exc
    except signing.BadSignature as exc:
        raise _invalid_confirmation(_("This confirmation is not valid.")) from exc
    if data.get("token") != token.pk or data.get("decision") not in DECISIONS:
        raise _invalid_confirmation(_("This confirmation is not valid."))
    return data


def _already_confirmed(day: AttendanceDay, action: str, nonce: str) -> bool:
    return AuditLog.objects.filter(
        content_type=ContentType.objects.get_for_model(AttendanceDay),
        object_id=str(day.pk),
        action=action,
        after__confirmation=nonce,
    ).exists()


@dataclass
class DecisionResult:
    day: AttendanceDay
    decision: str
    replayed: bool


def confirm_decision(token: AgentToken, confirmation: str) -> DecisionResult:
    """Take the decision previewed by ``preview_decision``."""
    data = _read_confirmation(token, confirmation)
    user, decision = token.user, data["decision"]
    day = scoped_day(user, data["day"])
    action = f"agents.day.{decision}"
    if _already_confirmed(day, action, data["nonce"]):
        return DecisionResult(day=day, decision=decision, replayed=True)
    _check_may_decide(token, day)
    if day_fingerprint(day) != data["fingerprint"]:
        raise Conflict(
            _("The roll call changed after the preview. Preview it again before deciding."),
            details={"status": day.status},
        )
    if decision == APPROVE:
        attendance.approve_day(day, user)
    else:
        attendance.reject_day(day, user, data["note"])
    AuditLog.record(
        actor=user,
        action=action,
        target=day,
        after={
            "agent_token": token.name,
            "token_prefix": token.prefix,
            "confirmation": data["nonce"],
        },
        reason=data["note"],
    )
    return DecisionResult(day=day, decision=decision, replayed=False)
