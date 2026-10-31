"""Company settings changes and audit-log helpers."""

import datetime
import json
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from .models import AuditLog, CompanySettings

SETTINGS_UPDATE_ACTION = "settings.update"
CURRENCY_FIELDS = ("currency", "currency_decimals")
_SKIPPED = {"id", "created_at", "updated_at"}


def _jsonable(value):
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        # "1.50" (database) and "1.5" (typed) are the same setting.
        return format(value.normalize(), "f")
    return value


def settings_snapshot(company: CompanySettings) -> dict:
    """Every setting as JSON-friendly values (times as "HH:MM", decimals as strings)."""
    return {
        field.name: _jsonable(getattr(company, field.attname))
        for field in CompanySettings._meta.concrete_fields
        if field.name not in _SKIPPED
    }


@dataclass(frozen=True)
class SettingsChange:
    changed: tuple[str, ...]
    entry: AuditLog | None

    @property
    def currency_changed(self) -> bool:
        return any(name in self.changed for name in CURRENCY_FIELDS)


def save_company_settings(form, *, actor, before: dict) -> SettingsChange:
    """Save a valid ``CompanySettingsForm`` and record the changed values in the audit log.

    ``before`` is ``settings_snapshot()`` of the row taken before the form was validated
    (validation already copies the new values onto the instance). Nothing is written when
    nothing changed.
    """
    company = form.instance
    after = settings_snapshot(company)
    changed = tuple(name for name in after if after[name] != before.get(name))
    if not changed:
        return SettingsChange(changed=(), entry=None)
    with transaction.atomic():
        form.save()
        entry = AuditLog.record(
            actor=actor,
            action=SETTINGS_UPDATE_ACTION,
            target=company,
            before={name: before.get(name) for name in changed},
            after={name: after[name] for name in changed},
        )
    return SettingsChange(changed=changed, entry=entry)


@dataclass(frozen=True)
class DiffRow:
    key: str
    before: str
    after: str
    changed: bool


def _show(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def audit_diff(before, after) -> list[DiffRow]:
    """Rows for the audit log viewer: one per key of ``before``/``after`` dicts.

    Non-dict values (a list, a number) give a single row keyed "value".
    """
    if not isinstance(before, dict | type(None)) or not isinstance(after, dict | type(None)):
        return [DiffRow("value", _show(before), _show(after), before != after)]
    before, after = before or {}, after or {}
    keys = list(before) + [key for key in after if key not in before]
    return [
        DiffRow(
            key=str(key),
            before=_show(before.get(key)),
            after=_show(after.get(key)),
            changed=before.get(key) != after.get(key),
        )
        for key in keys
    ]
