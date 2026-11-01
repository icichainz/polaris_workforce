"""A per-company marker that moves whenever attendance or pay changes.

Every change to attendance or pay writes an ``AuditLog`` entry, and ``AuditLog.record``
moves this marker. A screen that recomputes figures on read can remember the marker of
its last recomputation and skip the work while the marker has not moved.

The marker lives in the shared cache, so every worker process sees it. It moves when the
entry is written and again when the transaction commits: a reader that recomputed between
the two (from data not yet committed) sees it move once more. A marker missing from the
cache (cleared, culled) comes back as a new value, which counts as a change.
"""

from __future__ import annotations

import uuid

from django.core.cache import cache
from django.db import transaction

_KEY = "core:changes:{company_id}"


def _key(company_id: int) -> str:
    return _KEY.format(company_id=company_id)


def _move(company_id: int) -> None:
    cache.set(_key(company_id), uuid.uuid4().hex, timeout=None)


def mark_changed(company_id: int | None) -> None:
    """Attendance or pay of the company changed (or is changing in this transaction)."""
    if company_id is None:
        return
    _move(company_id)
    transaction.on_commit(lambda: _move(company_id))


def version(company_id: int) -> str:
    """The company's current marker (an opaque string)."""
    return cache.get_or_set(_key(company_id), lambda: uuid.uuid4().hex, timeout=None)
