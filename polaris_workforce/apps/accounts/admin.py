from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User

POLARIS_FIELDSET = (_("Polaris Workforce"), {"fields": ("role", "phone")})


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "last_name", "role", "phone", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    fieldsets = BaseUserAdmin.fieldsets + (POLARIS_FIELDSET,)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (POLARIS_FIELDSET,)
