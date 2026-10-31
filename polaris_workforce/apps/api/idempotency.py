"""Idempotent writes from the phone.

The rule (CLAUDE.md §7): **every write from the phone carries a ``client_uuid``**, made
on the phone when the user acts (before any network). The phone keeps writes in an
outbox and may send the same one several times (timeouts, retries after a crash, a
lost response). The server must apply it once:

* **Creates** (``POST /payments``, ``POST /records/{id}/corrections``): the first call
  creates the row and answers ``201``. A repeat with the same ``client_uuid`` creates
  nothing and answers ``200`` with the original result, plus the header
  ``Idempotent-Replayed: true``. If the ``client_uuid`` was already used for a
  *different* request (another pay line, another record), answer ``409 conflict``.
* **Upserts** (``PUT /sites/{id}/days/{date}/records``): each record carries its own
  ``client_uuid``; the server finds the record by ``client_uuid`` (then by worker),
  writes only the fields that changed and answers ``200``. Replaying the same batch
  changes nothing and writes no audit entry.
* **Actions** (``POST .../submit``): repeating an action that already happened with the
  same ``client_uuid`` answers ``200`` with the current state.

Usage in a view (Wave 2)::

    class PaymentCreateView(IdempotentCreateMixin, APIView):
        def post(self, request):
            serializer = PaymentCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            result = run_once(
                Payment.objects,                       # unscoped manager, see below
                data["client_uuid"],
                create=lambda: services.record_payment(request.user, **data),
                matches=lambda p: p.pay_line_id == data["pay_line_id"]
                and p.paid_by_id == request.user.pk,
            )
            return self.idempotent_response(result, PaymentSerializer(result.instance).data)

Pass the model's *unscoped* manager: if the queryset were filtered to the user's sites,
a ``client_uuid`` used elsewhere would not be found, the unique constraint would fail
and the user would get a 500 instead of a ``409``. Use ``matches`` for ownership.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, status
from rest_framework.response import Response

from .exceptions import Conflict

REPLAY_HEADER = "Idempotent-Replayed"
CLIENT_UUID_HELP = _(
    "Made on the phone when the user acts (UUID v4). Sending the same client_uuid again "
    "returns the original result instead of applying the write twice."
)


def client_uuid_field(**kwargs) -> serializers.UUIDField:
    """The ``client_uuid`` field every phone write serializer uses."""
    kwargs.setdefault("help_text", CLIENT_UUID_HELP)
    return serializers.UUIDField(**kwargs)


@dataclass(frozen=True)
class IdempotentResult[T]:
    instance: T
    created: bool

    @property
    def status_code(self) -> int:
        return status.HTTP_201_CREATED if self.created else status.HTTP_200_OK


def run_once[T](
    queryset,
    client_uuid: UUID | str,
    create: Callable[[], T],
    *,
    matches: Callable[[T], bool] | None = None,
) -> IdempotentResult[T]:
    """Return the object created for ``client_uuid``, creating it with ``create()`` once.

    ``create`` runs inside a savepoint. If two copies of the same write race, the loser
    hits the unique constraint on ``client_uuid`` and gets the winner's object back.
    Raises ``Conflict`` when the stored object does not ``match`` this request.
    """
    existing = queryset.filter(client_uuid=client_uuid).first()
    if existing is None:
        try:
            with transaction.atomic():
                return IdempotentResult(create(), created=True)
        except IntegrityError:
            existing = queryset.filter(client_uuid=client_uuid).first()
            if existing is None:
                raise  # Another constraint failed; not a replay.
    if matches is not None and not matches(existing):
        raise Conflict(
            _("This client_uuid was already used for a different request."),
            details={"client_uuid": str(client_uuid)},
        )
    return IdempotentResult(existing, created=False)


def apply_changes(instance: Any, values: dict[str, Any], fields: Iterable[str] | None = None):
    """Set the attributes of ``instance`` that differ from ``values``; return their names.

    For upserts: save (and write an AuditLog entry) only when the list is not empty, so a
    replayed batch is a no-op.
    """
    changed = []
    for name in fields if fields is not None else values:
        if name in values and getattr(instance, name) != values[name]:
            setattr(instance, name, values[name])
            changed.append(name)
    return changed


class IdempotentCreateMixin:
    """For views: answer 201 for a new object, 200 plus ``Idempotent-Replayed`` for a replay."""

    def idempotent_response(self, result: IdempotentResult, data) -> Response:
        response = Response(data, status=result.status_code)
        if not result.created:
            response[REPLAY_HEADER] = "true"
        return response
