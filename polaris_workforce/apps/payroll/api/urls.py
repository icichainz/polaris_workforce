"""Included once by ``apps/api/urls.py`` under ``/api/v1/``. Names: ``api:payroll:<name>``."""

from django.urls import path

from apps.api import converters  # noqa: F401  (registers <isodate:...>)

from . import views

app_name = "payroll"

urlpatterns = [
    path(
        "sites/<int:site_id>/payouts/<isodate:date>/",
        views.PayoutView.as_view(),
        name="payout",
    ),
    path("payments/", views.PaymentCreateView.as_view(), name="payments"),
]
