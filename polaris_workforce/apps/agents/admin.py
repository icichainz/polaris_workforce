from django.contrib import admin

from .models import AgentToken


@admin.register(AgentToken)
class AgentTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "user", "company", "can_approve", "expires_at", "revoked_at")
    list_filter = ("can_approve", "company")
    search_fields = ("name", "prefix", "user__username")
    # Tokens are created on the office screen (shown once); the admin only looks.
    readonly_fields = [field.name for field in AgentToken._meta.fields]

    def has_add_permission(self, request):
        return False
