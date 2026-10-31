from django.contrib import admin

from .models import (
    Advance,
    AdvanceRepayment,
    Bonus,
    CashIssue,
    DeductionRule,
    PayAdjustment,
    PayAdjustmentApplication,
    PayLine,
    Payment,
    PayPeriod,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    TaskType,
)


class PayLineInline(admin.TabularInline):
    model = PayLine
    extra = 0
    fields = (
        "worker",
        "units",
        "hours",
        "overtime_hours",
        "gross",
        "advance_deduction",
        "other_deductions",
        "adjustment",
        "net",
    )
    autocomplete_fields = ("worker",)
    show_change_link = True


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("method", "amount", "paid_by", "paid_at", "proof_type", "proof_file")
    autocomplete_fields = ("paid_by",)


class AdvanceRepaymentInline(admin.TabularInline):
    model = AdvanceRepayment
    extra = 0
    fields = ("amount", "pay_line", "created_at")
    readonly_fields = ("created_at",)
    raw_id_fields = ("pay_line",)


@admin.register(PayPeriod)
class PayPeriodAdmin(admin.ModelAdmin):
    list_display = ("site", "start_date", "end_date", "frequency", "status", "currency")
    list_filter = ("status", "frequency", "site")
    search_fields = ("site__name",)
    list_select_related = ("site",)
    autocomplete_fields = ("site",)
    date_hierarchy = "start_date"
    readonly_fields = ("locked_at",)
    inlines = [PayLineInline]


@admin.register(PayLine)
class PayLineAdmin(admin.ModelAdmin):
    list_display = ("worker", "period", "units", "gross", "advance_deduction", "adjustment", "net")
    list_filter = ("period__status", "period__site")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name")
    list_select_related = ("worker", "period__site")
    autocomplete_fields = ("worker", "period")
    inlines = [PaymentInline]


@admin.register(Advance)
class AdvanceAdmin(admin.ModelAdmin):
    list_display = ("worker", "amount", "deduction_per_pay", "balance", "given_on", "status")
    list_filter = ("status", "given_on", "proof_type")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name", "reason")
    list_select_related = ("worker",)
    autocomplete_fields = ("worker", "given_by")
    date_hierarchy = "given_on"
    inlines = [AdvanceRepaymentInline]


@admin.register(AdvanceRepayment)
class AdvanceRepaymentAdmin(admin.ModelAdmin):
    list_display = ("advance", "amount", "pay_line", "created_at")
    list_filter = ("created_at",)
    search_fields = ("advance__worker__number", "advance__worker__last_name")
    list_select_related = ("advance__worker", "pay_line")
    raw_id_fields = ("advance", "pay_line")


@admin.register(CashIssue)
class CashIssueAdmin(admin.ModelAdmin):
    list_display = ("date", "site", "amount", "issued_by", "received_by")
    list_filter = ("site", "date")
    search_fields = ("site__name", "note", "received_by__username", "received_by__last_name")
    list_select_related = ("site", "issued_by", "received_by")
    autocomplete_fields = ("site", "issued_by", "received_by")
    date_hierarchy = "date"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("paid_at", "pay_line", "method", "amount", "proof_type", "paid_by")
    list_filter = ("method", "proof_type", "paid_at")
    search_fields = (
        "pay_line__worker__number",
        "pay_line__worker__last_name",
        "client_uuid",
    )
    list_select_related = ("pay_line__worker", "pay_line__period__site", "paid_by")
    autocomplete_fields = ("paid_by",)
    raw_id_fields = ("pay_line",)
    date_hierarchy = "paid_at"


class PayAdjustmentApplicationInline(admin.TabularInline):
    model = PayAdjustmentApplication
    extra = 0
    fields = ("amount", "pay_line", "created_at")
    readonly_fields = ("created_at",)
    raw_id_fields = ("pay_line",)


@admin.register(PayAdjustment)
class PayAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("worker", "site", "amount", "applied_amount", "currency", "created_at")
    list_filter = ("site", "created_at")
    search_fields = ("worker__number", "worker__first_name", "worker__last_name", "reason")
    list_select_related = ("worker", "site")
    raw_id_fields = ("worker", "site", "source_line", "applied_to", "day", "record")
    autocomplete_fields = ("created_by",)
    inlines = [PayAdjustmentApplicationInline]


@admin.register(PayAdjustmentApplication)
class PayAdjustmentApplicationAdmin(admin.ModelAdmin):
    list_display = ("adjustment", "pay_line", "amount", "created_at")
    list_select_related = ("adjustment__worker", "pay_line__period__site")
    raw_id_fields = ("adjustment", "pay_line")


# --- pay rules (Wave 4) ----------------------------------------------------------------------


@admin.register(PayRuleSettings)
class PayRuleSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "sunday_multiplier", "holiday_multiplier", "night_multiplier")
    list_select_related = ("company",)


@admin.register(SitePayRules)
class SitePayRulesAdmin(admin.ModelAdmin):
    list_display = ("site", "sunday_multiplier", "holiday_multiplier", "night_multiplier")
    list_select_related = ("site",)
    raw_id_fields = ("site",)


@admin.register(DeductionRule)
class DeductionRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "employee_percent",
        "employer_percent",
        "active_from",
        "active_to",
        "order",
    )
    list_filter = ("company",)
    search_fields = ("name",)


@admin.register(TaskType)
class TaskTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "unit", "unit_rate", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name",)


@admin.register(PieceWorkEntry)
class PieceWorkEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "worker", "site", "task", "quantity", "unit_rate")
    list_select_related = ("worker", "site", "task")
    raw_id_fields = ("worker", "site", "task", "day", "entered_by")
    date_hierarchy = "date"


@admin.register(Bonus)
class BonusAdmin(admin.ModelAdmin):
    list_display = ("date", "worker", "site", "amount", "reason", "applied_to")
    list_select_related = ("worker", "site")
    raw_id_fields = ("worker", "site", "applied_to", "created_by")
    date_hierarchy = "date"


@admin.register(StoppageDay)
class StoppageDayAdmin(admin.ModelAdmin):
    list_display = ("date", "site", "reason", "pay", "percent", "amount")
    list_select_related = ("site",)
    raw_id_fields = ("site", "created_by")
    date_hierarchy = "date"
