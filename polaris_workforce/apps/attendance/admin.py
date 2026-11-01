from django.contrib import admin

from apps.core.admin import AuditedAdmin

from .models import AttendanceCorrection, AttendanceDay, AttendanceRecord


class AttendanceRecordInline(admin.TabularInline):
    model = AttendanceRecord
    extra = 0
    fields = ("worker", "status", "arrival", "departure", "note")
    autocomplete_fields = ("worker",)


class AttendanceCorrectionInline(admin.TabularInline):
    model = AttendanceCorrection
    extra = 0
    fields = ("field", "old_value", "new_value", "reason", "corrected_by", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("corrected_by",)


@admin.register(AttendanceDay)
class AttendanceDayAdmin(AuditedAdmin):
    list_display = ("date", "site", "foreman", "status", "submitted_at", "approved_by")
    list_filter = ("status", "site", "date")
    search_fields = ("site__name", "foreman__username", "foreman__last_name", "foreman_note")
    list_select_related = ("site", "foreman", "approved_by")
    autocomplete_fields = ("site", "foreman", "approved_by")
    date_hierarchy = "date"
    inlines = [AttendanceRecordInline]


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(AuditedAdmin):
    list_display = ("day", "worker", "status", "arrival", "departure", "recorded_at")
    list_filter = ("status", "day__site", "day__date")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name", "client_uuid")
    list_select_related = ("day__site", "worker")
    autocomplete_fields = ("day", "worker")
    inlines = [AttendanceCorrectionInline]


@admin.register(AttendanceCorrection)
class AttendanceCorrectionAdmin(AuditedAdmin):
    list_display = ("created_at", "record", "field", "old_value", "new_value", "corrected_by")
    list_filter = ("field", "created_at")
    search_fields = ("record__worker__number", "record__worker__last_name", "reason")
    list_select_related = ("record__worker", "record__day__site", "corrected_by")
    autocomplete_fields = ("record", "corrected_by")
    readonly_fields = ("created_at",)
