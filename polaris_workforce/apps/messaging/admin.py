"""Django admin (platform admins only, every company): read the outbox and settings."""

from django.contrib import admin

from .models import ContactPreference, Message, MessagingSettings


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "kind", "channel", "to", "status", "attempts")
    list_filter = ("company", "status", "kind", "channel")
    search_fields = ("to", "text", "dedupe_key")
    readonly_fields = ("created_at", "sent_at", "updated_at")
    raw_id_fields = ("user", "worker")


@admin.register(MessagingSettings)
class MessagingSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "enabled", "sms_enabled", "whatsapp_enabled", "default_channel")


@admin.register(ContactPreference)
class ContactPreferenceAdmin(admin.ModelAdmin):
    list_display = ("company", "user", "worker", "opted_in", "channel", "updated_at")
    list_filter = ("company", "opted_in")
    raw_id_fields = ("user", "worker", "updated_by")
