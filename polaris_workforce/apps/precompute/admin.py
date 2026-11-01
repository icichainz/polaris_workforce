from django.contrib import admin

from .models import Freshness, RefreshRun, Stale


class _ReadOnly(admin.ModelAdmin):
    """Written by the service only (``apps.precompute.services``)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RefreshRun)
class RefreshRunAdmin(_ReadOnly):
    list_display = ("started_at", "company", "sweep", "sites", "finished_at")
    list_filter = ("sweep", "company")
    list_select_related = ("company",)
    readonly_fields = ("company", "sweep", "started_at", "finished_at", "sites", "mismatches")


@admin.register(Stale)
class StaleAdmin(_ReadOnly):
    list_display = ("site", "start", "end", "version", "updated_at")
    list_select_related = ("site",)


@admin.register(Freshness)
class FreshnessAdmin(_ReadOnly):
    list_display = ("site", "fresh_on")
    list_select_related = ("site",)
