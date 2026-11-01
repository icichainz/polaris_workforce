"""Settings inheritance: company → site → worker.

Every rule is a ``CompanySettings`` field. Some of them can be overridden on a ``Site``
and a few on a ``Worker``; an override field has the same name as the company field and
is ``None`` (or empty) when it inherits. Read settings only through ``resolve_setting``.
"""

from typing import Any

from .models import CompanySettings

# Names that a Site may override (Site has a nullable field with the same name).
SITE_OVERRIDABLE: frozenset[str] = frozenset(
    {
        "pay_frequency",
        "pay_every_n_days",
        "payment_method",
        "work_start",
        "work_end",
        "late_after",
        "roll_call_due",
        "overtime_after_hours",
        "overtime_multiplier",
        # Presence tracking (apps.presence)
        "tracking_enabled",
        "presence_sightings_to_enter",
        "presence_arrival_window_seconds",
        "presence_absence_to_leave_seconds",
        "foreman_gps_interval_seconds",
        "tracking_start",
        "tracking_end",
    }
)

# Names that a Worker may override (Worker has a nullable field with the same name).
WORKER_OVERRIDABLE: frozenset[str] = frozenset(
    {"payment_method", "pay_frequency", "tracking_enabled"}
)

# The presence-tracking settings: changing one pushes the site configs to the tracker
# (``apps.presence``). ``track_retention_days`` is company-wide only.
TRACKING_SETTINGS: frozenset[str] = frozenset(
    {
        "tracking_enabled",
        "presence_sightings_to_enter",
        "presence_arrival_window_seconds",
        "presence_absence_to_leave_seconds",
        "foreman_gps_interval_seconds",
        "tracking_start",
        "tracking_end",
        "track_retention_days",
    }
)

_EXCLUDED_FIELDS = {"id", "company", "created_at", "updated_at"}


def setting_names() -> frozenset[str]:
    """Every name ``resolve_setting`` accepts (the CompanySettings field names)."""
    return frozenset(
        field.name
        for field in CompanySettings._meta.concrete_fields
        if field.name not in _EXCLUDED_FIELDS
    )


def _is_set(value: Any) -> bool:
    return value is not None and value != ""


def resolve_setting(name: str, site=None, worker=None, *, company: CompanySettings | None = None):
    """Return the effective value of setting ``name``.

    The worker override wins if set, then the site override, then the company value.
    ``company`` may be passed to avoid reloading ``CompanySettings`` in a loop; without
    it, the settings of the site's (or worker's) company are read, else the current
    company's. Unknown names raise ``KeyError``.
    """
    if name not in setting_names():
        raise KeyError(name)
    if worker is not None and name in WORKER_OVERRIDABLE:
        value = getattr(worker, name)
        if _is_set(value):
            return value
    if site is not None and name in SITE_OVERRIDABLE:
        value = getattr(site, name)
        if _is_set(value):
            return value
    if company is None:
        owner = getattr(site, "company_id", None) or getattr(worker, "company_id", None)
        company = CompanySettings.load(owner)
    return getattr(company, name)
