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
class CompanySettingsAdmin(admin.ModelAdmin):
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
