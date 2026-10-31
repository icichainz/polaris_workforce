from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import PayRate, Trade, Worker


class PayRateInline(admin.TabularInline):
    model = PayRate
    extra = 0
    fields = ("pay_type", "amount", "effective_from")
    ordering = ("-effective_from",)


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("number", "last_name", "first_name", "trade", "phone", "is_active")
    list_filter = ("is_active", "trade", "id_document_type")
    search_fields = ("number", "first_name", "last_name", "phone", "id_document_number")
    list_select_related = ("trade",)
    inlines = [PayRateInline]
    fieldsets = (
        (None, {"fields": ("number", "first_name", "last_name", "phone", "photo", "trade")}),
        (_("Identity document"), {"fields": ("id_document_type", "id_document_number")}),
        (_("Status"), {"fields": ("is_active",)}),
        (
            _("Setting overrides (empty = site or company setting)"),
            {"classes": ("collapse",), "fields": ("payment_method", "pay_frequency")},
        ),
    )


@admin.register(PayRate)
class PayRateAdmin(admin.ModelAdmin):
    list_display = ("worker", "pay_type", "amount", "effective_from")
    list_filter = ("pay_type", "effective_from")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name")
    list_select_related = ("worker",)
    autocomplete_fields = ("worker",)
    date_hierarchy = "effective_from"
