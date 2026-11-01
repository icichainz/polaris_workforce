"""Django admin: platform admins only.

The admin shows every company's data, so only platform admins (superusers without a
company, ``apps.core.tenancy``) may open it. Company admins — including superusers who
belong to a company — use the office screens, which are scoped to their company.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from . import services
from .models import AuditLog, Company, CompanySettings
from .tenancy import is_platform_admin


def _platform_admins_only(request) -> bool:
    user = request.user
    return user.is_active and user.is_staff and is_platform_admin(user)


admin.site.has_permission = _platform_admins_only


# Paths from a record to its company, tried in order (sites, workers, days, pay…).
_COMPANY_PATHS = (
    ("company_id",),
    ("site", "company_id"),
    ("worker", "company_id"),
    ("day", "site", "company_id"),
    ("period", "site", "company_id"),
    ("record", "day", "site", "company_id"),
    ("pay_line", "period", "site", "company_id"),
)


def _company_of(obj) -> int | None:
    for path in _COMPANY_PATHS:
        value = obj
        for name in path:
            value = getattr(value, name, None)
            if value is None:
                break
        if value is not None:
            return value
    return None


class AuditedAdmin(admin.ModelAdmin):
    """An admin whose edits write audit entries (``admin.<app>.<model>.<add|change|delete>``),
    inline rows included, like every other change to attendance or pay (and so the
    figures built from them are marked out of date: ``apps.precompute``). Deletions are
    recorded before the row goes, while what it belonged to can still be read; a change
    also before it is saved (``…change_from``), so that the place it moves away from (an
    earlier date, another site) is marked as well as the new one."""

    def _audit(self, request, obj, verb: str, fields=None) -> None:
        AuditLog.record(
            actor=request.user,
            action=f"admin.{obj._meta.label_lower}.{verb}",
            target=obj,
            after={"fields": sorted(fields)} if fields else None,
            company=_company_of(obj),
        )

    def save_model(self, request, obj, form, change):
        if change:
            self._audit(request, obj, "change_from", form.changed_data)
        super().save_model(request, obj, form, change)
        self._audit(request, obj, "change" if change else "add", form.changed_data)

    def delete_model(self, request, obj):
        self._audit(request, obj, "delete")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self._audit(request, obj, "delete")
        super().delete_queryset(request, queryset)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            self._audit(request, obj, "delete")
            obj.delete()
        changed = {id(obj): fields for obj, fields in formset.changed_objects}
        for obj in instances:
            is_new = obj.pk is None
            if not is_new:
                self._audit(request, obj, "change_from", changed.get(id(obj)))
            obj.save()
            self._audit(request, obj, "add" if is_new else "change", changed.get(id(obj)))
        formset.save_m2m()


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    readonly_fields = ("created_at",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            services.set_up_company(obj, actor=request.user)

    def has_delete_permission(self, request, obj=None):
        return False  # a company keeps its records; make it inactive instead


@admin.register(CompanySettings)
class CompanySettingsAdmin(AuditedAdmin):
    fieldsets = (
        (
            _("Company"),
            {
                "fields": (
                    "company",
                    "company_name",
                    "country",
                    "currency",
                    "currency_decimals",
                    "number_format",
                    "time_zone",
                )
            },
        ),
        (_("Language"), {"fields": ("language",)}),
        (
            _("Attendance"),
            {
                "fields": (
                    "attendance_mode",
                    "working_days",
                    "work_start",
                    "work_end",
                    "break_minutes",
                    "late_after",
                    "roll_call_due",
                    "late_rule",
                    "half_day_units",
                    "require_correction_reason",
                    "record_times",
                    "public_holidays",
                )
            },
        ),
        (
            _("Pay"),
            {
                "fields": (
                    "pay_frequency",
                    "pay_every_n_days",
                    "default_pay_type",
                    "payment_method",
                    "overtime_after_hours",
                    "overtime_multiplier",
                    "accepted_proofs",
                )
            },
        ),
        (_("Advances"), {"fields": ("advance_limit_percent", "default_advance_deduction")}),
        (
            _("Presence tracking"),
            {
                "classes": ("collapse",),
                "fields": (
                    "tracking_enabled",
                    "tracking_start",
                    "tracking_end",
                    "presence_sightings_to_enter",
                    "presence_arrival_window_seconds",
                    "presence_absence_to_leave_seconds",
                    "foreman_gps_interval_seconds",
                    "track_retention_days",
                ),
            },
        ),
    )
    list_display = (
        "__str__",
        "company",
        "currency",
        "currency_decimals",
        "pay_frequency",
        "updated_at",
    )
    list_select_related = ("company",)
    readonly_fields = ("company",)

    # One row per company, created with the company.
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "actor", "action", "content_type", "object_id")
    list_filter = ("company", "action", "content_type")
    search_fields = ("action", "object_id", "reason", "actor__username")
    date_hierarchy = "created_at"
    list_select_related = ("company", "actor", "content_type")
    readonly_fields = (
        "company",
        "actor",
        "action",
        "content_type",
        "object_id",
        "before",
        "after",
        "reason",
        "created_at",
    )

    # The audit log is append-only.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
