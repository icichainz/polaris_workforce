from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .models import AuditLog, CompanySettings


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            _("Company"),
            {
                "fields": (
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
    list_display = ("__str__", "currency", "currency_decimals", "pay_frequency", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # There is only one row: go straight to it.
        obj = CompanySettings.load()
        return redirect(reverse("admin:core_companysettings_change", args=[obj.pk]))


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "content_type", "object_id")
    list_filter = ("action", "content_type")
    search_fields = ("action", "object_id", "reason", "actor__username")
    date_hierarchy = "created_at"
    list_select_related = ("actor", "content_type")
    readonly_fields = (
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
