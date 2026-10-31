from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import rule_views, views

app_name = "payroll"

urlpatterns = [
    path("cash-sheets/", views.cash_sheets, name="cash_sheets"),
    path(
        "cash-sheets/<int:site_id>/<isodate:date>/",
        views.cash_sheet,
        name="cash_sheet",
    ),
    path(
        "cash-sheets/<int:site_id>/<isodate:date>/cash/",
        views.cash_issue,
        name="cash_issue",
    ),
    path(
        "cash-sheets/<int:site_id>/<isodate:date>/lock/",
        views.cash_sheet_lock,
        name="cash_sheet_lock",
    ),
    path("cash-sheets/lines/<int:line_id>/pay/", views.cash_sheet_pay, name="cash_sheet_pay"),
    path("cash-sheets/payments/<int:payment_id>/undo/", views.payment_undo, name="payment_undo"),
    path("pay-periods/", views.periods, name="periods"),
    path("payslips/<int:line_id>/", views.payslip, name="payslip"),
    path("advances/", views.advances, name="advances"),
    path("advances/preview/", views.advance_preview, name="advance_preview"),
    # Pay rules (Wave 4)
    path("pay-rules/", rule_views.rules, name="pay_rules"),
    path("pay-rules/deductions/new/", rule_views.deduction_rule, name="deduction_rule_create"),
    path("pay-rules/deductions/<int:pk>/", rule_views.deduction_rule, name="deduction_rule_edit"),
    path("pay-rules/tasks/new/", rule_views.task_type, name="task_type_create"),
    path("pay-rules/tasks/<int:pk>/", rule_views.task_type, name="task_type_edit"),
    path("pay-rules/sites/<int:site_id>/", rule_views.site_rules, name="site_pay_rules"),
    path("day-extras/<int:site_id>/<isodate:date>/", rule_views.day_extras, name="day_extras"),
    path(
        "day-extras/piece-work/<int:pk>/delete/",
        rule_views.piece_work_delete,
        name="piece_work_delete",
    ),
    path("day-extras/bonuses/<int:pk>/delete/", rule_views.bonus_delete, name="bonus_delete"),
]
