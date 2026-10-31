from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import ForemanDevice, User

POLARIS_FIELDSET = (_("Polaris Workforce"), {"fields": ("role", "phone", "language")})


class ForemanDeviceInline(admin.TabularInline):
    model = ForemanDevice
    extra = 0
    fields = ("device_id", "failed_attempts", "locked_until", "last_seen_at", "revoked_at")
    readonly_fields = ("device_id", "last_seen_at")
    show_change_link = True


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "last_name", "role", "phone", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    fieldsets = BaseUserAdmin.fieldsets + (POLARIS_FIELDSET,)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (POLARIS_FIELDSET,)
    inlines = [ForemanDeviceInline]


@admin.register(ForemanDevice)
class ForemanDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "device_id",
        "failed_attempts",
        "locked_until",
        "last_seen_at",
        "revoked_at",
        "created_at",
    )
    list_filter = ("revoked_at", "created_at")
    search_fields = ("user__username", "user__first_name", "user__last_name", "device_id")
    list_select_related = ("user",)
    autocomplete_fields = ("user",)
    # Hashes are never edited by hand: tokens and PINs are issued by the API.
    readonly_fields = ("token_hash", "pin_hash", "created_at", "last_seen_at")
