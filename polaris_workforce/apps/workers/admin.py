from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import PayRate, Trade, Worker, WorkerDocument


class PayRateInline(admin.TabularInline):
    model = PayRate
    extra = 0
    fields = ("pay_type", "amount", "effective_from")
    ordering = ("-effective_from",)


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ("name", "company")
    list_filter = ("company",)
    search_fields = ("name",)


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("number", "last_name", "first_name", "company", "trade", "phone", "is_active")
    list_filter = ("company", "is_active", "id_document_type")
    search_fields = ("number", "first_name", "last_name", "phone", "id_document_number")
    list_select_related = ("company", "trade")
    inlines = [PayRateInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    "number",
                    "first_name",
                    "last_name",
                    "phone",
                    "photo",
                    "trade",
                )
            },
        ),
        (_("Identity document"), {"fields": ("id_document_type", "id_document_number")}),
        (
            _("Emergency contact"),
            {
                "fields": (
                    "emergency_contact_name",
                    "emergency_contact_relationship",
                    "emergency_contact_phone",
                )
            },
        ),
        (_("Status"), {"fields": ("is_active",)}),
        (
            _("Setting overrides (empty = site or company setting)"),
            {"classes": ("collapse",), "fields": ("payment_method", "pay_frequency")},
        ),
    )


@admin.register(WorkerDocument)
class WorkerDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "worker", "type", "expires_on", "uploaded_at")
    list_filter = ("type", "worker__company")
    search_fields = ("title", "worker__number", "worker__first_name", "worker__last_name")
    list_select_related = ("worker",)
    autocomplete_fields = ("worker",)
    readonly_fields = ("uploaded_by", "uploaded_at")
    date_hierarchy = "uploaded_at"


@admin.register(PayRate)
class PayRateAdmin(admin.ModelAdmin):
    list_display = ("worker", "pay_type", "amount", "effective_from")
    list_filter = ("pay_type", "effective_from")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name")
    list_select_related = ("worker",)
    autocomplete_fields = ("worker",)
    date_hierarchy = "effective_from"
