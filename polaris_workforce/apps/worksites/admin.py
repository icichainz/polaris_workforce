from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Site, SiteAssignment


class SiteAssignmentInline(admin.TabularInline):
    model = SiteAssignment
    extra = 0
    fields = ("worker", "start_date", "end_date")
    autocomplete_fields = ("worker",)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "manager", "started_on", "labour_budget")
    list_filter = ("status", "started_on")
    search_fields = ("name", "address", "manager__username", "manager__last_name")
    list_select_related = ("manager",)
    autocomplete_fields = ("manager",)
    filter_horizontal = ("foremen",)
    inlines = [SiteAssignmentInline]
    fieldsets = (
        (None, {"fields": ("name", "address", "status", "started_on", "labour_budget")}),
        (_("People"), {"fields": ("manager", "foremen")}),
        (
            _("Setting overrides (empty = company setting)"),
            {
                "classes": ("collapse",),
                "fields": (
                    "pay_frequency",
                    "pay_every_n_days",
                    "payment_method",
                    "work_start",
                    "work_end",
                    "late_after",
                    "overtime_after_hours",
                    "overtime_multiplier",
                ),
            },
        ),
    )


@admin.register(SiteAssignment)
class SiteAssignmentAdmin(admin.ModelAdmin):
    list_display = ("worker", "site", "start_date", "end_date")
    list_filter = ("site", "start_date")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name", "site__name")
    list_select_related = ("worker", "site")
    autocomplete_fields = ("worker", "site")
    date_hierarchy = "start_date"
