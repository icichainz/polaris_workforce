"""Django admin for presence tracking. Edits of zones, gateways and badges are audited
(``AuditedAdmin``) and push the site configs (``signals``); events, sessions, consents and
push states are read only (the tracker and the phone write them)."""

from django.contrib import admin

from apps.core.admin import AuditedAdmin

from .models import (
    BleBadge,
    Gateway,
    PresenceEvent,
    PresenceReconciliation,
    PresenceSession,
    TrackerSync,
    TrackingConsent,
    Zone,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Zone)
class ZoneAdmin(AuditedAdmin):
    list_display = ("code", "name", "site")
    list_filter = ("site__company",)
    search_fields = ("code", "name", "site__name")
    list_select_related = ("site",)
    autocomplete_fields = ("site",)


@admin.register(Gateway)
class GatewayAdmin(AuditedAdmin):
    list_display = ("label", "site", "zone", "token_issued_at", "revoked_at")
    list_filter = ("site__company",)
    search_fields = ("label", "site__name")
    list_select_related = ("site", "zone")
    autocomplete_fields = ("site",)
    readonly_fields = (
        "token_generation",
        "token_issued_at",
        "token_issued_by",
        "revoked_at",
        "revoked_by",
    )


@admin.register(BleBadge)
class BleBadgeAdmin(AuditedAdmin):
    list_display = ("badge_id", "worker", "company", "active_from", "active_to")
    list_filter = ("company",)
    search_fields = ("badge_id", "worker__number", "worker__first_name", "worker__last_name")
    list_select_related = ("worker", "company")
    autocomplete_fields = ("worker",)


@admin.register(PresenceEvent)
class PresenceEventAdmin(ReadOnlyAdmin):
    list_display = ("at", "type", "subject_kind", "subject_id", "site_ref", "applied")
    list_filter = ("type", "applied", "company")
    search_fields = ("event_id",)
    date_hierarchy = "at"


@admin.register(PresenceSession)
class PresenceSessionAdmin(ReadOnlyAdmin):
    list_display = (
        "local_date",
        "site",
        "subject_kind",
        "subject_id",
        "started_at",
        "ended_at",
        "source",
    )
    list_filter = ("company", "source", "local_date")
    list_select_related = ("site",)
    date_hierarchy = "local_date"


@admin.register(TrackingConsent)
class TrackingConsentAdmin(ReadOnlyAdmin):
    list_display = ("user", "accepted", "text_version", "at")
    list_filter = ("accepted", "company")
    list_select_related = ("user",)


@admin.register(TrackerSync)
class TrackerSyncAdmin(ReadOnlyAdmin):
    list_display = ("site", "pushed_enabled", "last_success_at", "last_attempt_at", "last_error")
    list_select_related = ("site",)


@admin.register(PresenceReconciliation)
class PresenceReconciliationAdmin(ReadOnlyAdmin):
    list_display = (
        "local_date",
        "site",
        "reconciled_at",
        "added",
        "changed",
        "removed",
        "last_attempt_at",
        "last_error",
    )
    list_filter = ("company", "local_date")
    list_select_related = ("site",)
    date_hierarchy = "local_date"
