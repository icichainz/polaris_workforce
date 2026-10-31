from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

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
]
