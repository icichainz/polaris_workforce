"""Django admin (platform admins only, every company)."""

from django.contrib import admin

from .models import AnalyticsSettings


@admin.register(AnalyticsSettings)
class AnalyticsSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "budget_warn_percent", "updated_at")
    list_filter = ("company",)
